# Servicio de alertas: une las piezas. Expone 3 rutas y arranca 3 hilos de fondo.
#   POST /data   <- Orion notifica una lectura (fuera o dentro de rango)
#   GET  /estado -> estado actual en JSON
#   WS   /ws     -> el front recibe el estado al conectarse y cada transicion despues
#   hilo revisar  -> cada REVISION_S pregunta a Orion por la entidad (detecta SIN_DATOS)
#   hilo cartero  -> manda por SMTP los eventos pendientes de alertas_eventos
#   hilo difusor  -> manda por websocket los mensajes de la cola "difusion"
import hmac
import json
import os
import queue
import threading
import time
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request
from flask_sock import Sock

import almacen
from correo import Smtp, asunto, cuerpo, enviar
from ngsi import Lectura, leer_entidad, leer_notificacion
from reglas import Config, aplicar_lectura, estado_inicial, revisar_periodica
from suscripciones import CABECERAS

# Configuracion leida una vez al arrancar (variables de docker-compose / nube/.env).
# Si los umbrales no tienen sentido, Config lanza ValueError aqui y el contenedor no arranca
CFG = Config.desde_env()
SMTP = Smtp.desde_env()
PG_DSN = os.environ.get("PG_DSN", "host=postgres port=5432 dbname=plantas_db user=admin password=adminpassword")
ORION_URL = os.environ.get("ORION_URL", "http://orion:1026")
ENTIDAD = os.environ.get("ENTIDAD", "Plant:plant001")
FRONT_URL = os.environ.get("FRONT_URL", "")
ZONA = ZoneInfo(os.environ.get("ZONA", "America/Bogota"))
REVISION_S = int(os.environ.get("REVISION_S", "30"))
PENDIENTE_MAX_S = int(os.environ.get("PENDIENTE_MAX_S", "3600"))   # un correo que no salio en 1 h ya no se envia
TOKEN = os.environ.get("ALERTAS_TOKEN", "")   # obligatorio: /data exige ?token=<TOKEN> (Orion lo manda por httpCustom.qs)

app = Flask(__name__)
sock = Sock(app)
lock_estado = threading.Lock()   # cargar -> aplicar -> guardar sin carreras entre /data y la revision periodica
lock_ws = threading.Lock()       # protege "clientes", que tocan /ws (altas/bajas) y el difusor (envios)
clientes = set()                 # websockets abiertos (una pestaña del dashboard = un cliente)
# /data y la revision solo avisan; correo y websocket salen en sus propios hilos para que Orion no espere
hay_eventos = threading.Event()  # "timbre" para despertar al cartero: hay un evento nuevo en la BD
difusion = queue.Queue()         # mensajes para el difusor; Queue ya es segura entre hilos


def ahora_ms() -> int:
    return int(time.time() * 1000)


def mensaje(e, de=None) -> dict:
    # JSON que reciben el front (/ws) y /estado. "de" solo viene en una transicion (estado anterior)
    return {"entity_id": e.entity_id, "estado": e.estado, "desde": e.desde, "valor": e.valor,
            "umbral": e.umbral, "ultimo_ts": e.ultimo_ts, "de": de}


def estado_actual():
    conn = almacen.conectar(PG_DSN)
    try:
        return almacen.cargar_estado(conn, ENTIDAD) or estado_inicial(ENTIDAD, ahora_ms())
    finally:
        conn.close()


def difundir(msg: dict) -> None:
    texto = json.dumps(msg)
    with lock_ws:
        destino = list(clientes)
    for ws in destino:   # fuera del lock: un cliente trabado no bloquea las conexiones nuevas a /ws
        try:
            ws.send(texto)
        except Exception:
            with lock_ws:
                clientes.discard(ws)


def difusor() -> None:
    # unico hilo que difunde: simple-websocket no admite dos send a la vez sobre el mismo socket
    while True:
        difundir(difusion.get())


def enviar_pendientes() -> None:
    # Patron "outbox": la transicion se guardo en la BD junto con el estado; aqui se lee y se manda.
    # Si SMTP falla o el contenedor se reinicia, el evento sigue pendiente y se reintenta despues
    try:
        conn = almacen.conectar(PG_DSN)
        try:
            for evento_id, t in almacen.pendientes(conn):
                # sin destinatario o muy viejo: se descarta, para no mandar una rafaga atrasada despues
                motivo = "sin ALERTA_DESTINO" if not SMTP.destino else \
                    "pendiente hace mas de PENDIENTE_MAX_S" if ahora_ms() - t.ts > PENDIENTE_MAX_S * 1000 else None
                if motivo:
                    print(f"Evento {evento_id} ({t.de} -> {t.a}) descartado: {motivo}")
                    almacen.marcar_enviado(conn, evento_id, ahora_ms())
                    continue
                try:
                    titulo, texto = asunto(t, ZONA), cuerpo(t, CFG, FRONT_URL, ZONA)
                except Exception as e:
                    # evento imposible de armar: se descarta para no trabar la cola para siempre
                    print(f"Evento {evento_id} descartado: {e}")
                    almacen.marcar_enviado(conn, evento_id, ahora_ms())
                    continue
                if not enviar(titulo, texto, SMTP):
                    break   # fallo de SMTP: se reintenta en la siguiente vuelta del cartero, conservando el orden
                almacen.marcar_enviado(conn, evento_id, ahora_ms())
                print(f"Correo enviado: {titulo}")
        finally:
            conn.close()
    except Exception as e:
        print(f"Error enviando pendientes: {e}")


def cartero() -> None:
    # unico hilo que envia correos (un pendiente nunca sale dos veces). Despierta con cada transicion,
    # y cada REVISION_S reintenta los que fallaron
    while True:
        hay_eventos.wait(REVISION_S)   # duerme hasta que suene el timbre o pasen REVISION_S, lo que ocurra primero
        hay_eventos.clear()
        enviar_pendientes()


def actualizar(entity_id: str, paso) -> None:
    # paso(estado) -> (nuevo_estado, transicion | None)
    # Punto unico donde cambia el estado; lo usan /data (paso = aplicar_lectura) y revisar (paso = revisar_periodica).
    # Ciclo: cargar de la BD -> aplicar la regla pura -> guardar si cambio algo -> avisar a los hilos si hubo transicion
    with lock_estado:
        conn = almacen.conectar(PG_DSN)
        try:
            e = almacen.cargar_estado(conn, entity_id) or estado_inicial(entity_id, ahora_ms())
            nuevo, t = paso(e)
            if nuevo != e:
                almacen.guardar(conn, nuevo, t)
        finally:
            conn.close()
    if t:   # fuera del lock: avisar no necesita el estado bloqueado
        print(f"Transicion {entity_id}: {t.de} -> {t.a} (valor={t.valor})")
        difusion.put(mensaje(nuevo, t.de))   # -> difusor -> websocket
        hay_eventos.set()                    # -> cartero -> correo


@app.after_request
def cors(resp):
    # el dashboard se sirve desde otro puerto/origen y hace fetch a /estado
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.post("/data")
def data():
    # notificacion de Orion (suscripciones "fuera de rango" y "dentro de rango")
    # falla cerrado: sin ALERTAS_TOKEN configurado no se acepta ninguna notificacion
    # compare_digest compara en tiempo constante (no filtra por cuanto tarda cuantos caracteres acertaste)
    if not TOKEN or not hmac.compare_digest(request.args.get("token", "").encode(), TOKEN.encode()):
        return "", 403
    try:
        for l in leer_notificacion(request.get_json(force=True), ahora_ms()):
            if l.entity_id != ENTIDAD:   # solo plant001 (spec); evita alertas de dispositivos autoregistrados
                continue
            # l=l fija la lectura de esta vuelta del for dentro de la lambda
            actualizar(l.entity_id, lambda e, l=l: aplicar_lectura(e, l.valor, l.ts, CFG))
        return "", 204   # respuesta rapida: Orion no espera ni al correo ni al websocket
    except Exception as e:
        print(f"Error procesando notificacion: {e}")
        return "", 500


@app.get("/estado")
def estado():
    return jsonify(mensaje(estado_actual()))


@sock.route("/ws")
def ws(ws):
    # flask-sock dedica un hilo a cada conexion; este hilo vive mientras la pestaña este abierta
    with lock_ws:
        clientes.add(ws)
    try:
        ws.send(json.dumps(mensaje(estado_actual())))
        while True:
            ws.receive()   # el front no envia nada; esto solo detecta el cierre
    except Exception:
        pass
    finally:
        with lock_ws:
            clientes.discard(ws)


def revisar_una(ultima: int) -> int:
    # SIN_DATOS se mide con el TimeInstant de Orion: en la banda de histeresis ninguna suscripcion notifica.
    # Si Orion no tiene la entidad (404) o no responde, se mide desde la ultima trama que se le vio
    try:
        r = requests.get(f"{ORION_URL}/v2/entities/{ENTIDAD}", headers=CABECERAS, timeout=10)
        r.raise_for_status()
        l = leer_entidad(r.json(), ahora_ms())
        ultima = l.ts
    except Exception as e:
        print(f"Orion sin {ENTIDAD}: {e}")
        l = Lectura(ENTIDAD, None, ultima)
    actualizar(ENTIDAD, lambda e: revisar_periodica(e, l.valor, l.ts, ahora_ms(), CFG))
    return ultima


def revisar() -> None:
    ultima = ahora_ms()   # si Orion nunca responde, SIN_DATOS_S se cuenta desde el arranque
    while True:
        time.sleep(REVISION_S)
        try:
            ultima = revisar_una(ultima)
        except Exception as e:
            print(f"Error en revision periodica: {e}")


def iniciar_bd() -> None:
    # depends_on no espera a que Postgres acepte conexiones, solo a que el contenedor arranque: se reintenta
    while True:
        try:
            conn = almacen.conectar(PG_DSN)
            almacen.crear_tablas(conn)
            conn.close()
            return
        except Exception as e:
            print(f"Esperando Postgres: {e}")
            time.sleep(5)


if __name__ == "__main__":
    iniciar_bd()
    # daemon=True: los hilos mueren junto con el proceso principal (no impiden que el contenedor se detenga)
    for hilo in (revisar, cartero, difusor):
        threading.Thread(target=hilo, daemon=True).start()
    print(f"Alertas activas: {CFG}")
    if not TOKEN:
        print("ATENCION: ALERTAS_TOKEN vacio, /data rechaza todas las notificaciones")
    app.run(host="0.0.0.0", port=5050, threaded=True)
