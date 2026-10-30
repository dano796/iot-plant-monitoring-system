# Crea o actualiza las dos suscripciones de Orion que son la regla de la alerta.
# Se puede correr varias veces: busca por descripcion y nunca duplica ni toca otras (p. ej. la de QuantumLeap).
#   docker exec fiware-alertas python suscripciones.py
import os

import requests

from reglas import Config

# La descripcion funciona como "llave" de la suscripcion: asi se reconoce cual es nuestra al volver a correr
DESC_FUERA = "alertas: temperatura fuera de rango"
DESC_DENTRO = "alertas: temperatura dentro de rango"
# Orion separa los datos por "tenant" (servicio + ruta); el IoT Agent guarda plant001 en openiot, /
CABECERAS = {"fiware-service": "openiot", "fiware-servicepath": "/"}


# Los filtros q son el lenguaje de consulta de NGSI-v2: "a..b" es un rango, "!=" lo niega y ";" es un AND.
# Con los valores por defecto quedan:
#   fuera:  temperatura!=10..30;temperatura==0..60   -> notifica si esta en [0,10) o (30,60]
#   dentro: temperatura==12..28                      -> notifica si esta en [12,28]
# Lo que cae entre medio (10-12 y 28-30) es la banda de histeresis: ninguna de las dos notifica
def q_fuera(cfg: Config) -> str:
    # fuera del rango normal pero dentro del valido (un HDC1080 desconectado da 125 y se ignora)
    return f"temperatura!={cfg.temp_min:g}..{cfg.temp_max:g};temperatura=={cfg.valido_min:g}..{cfg.valido_max:g}"


def q_dentro(cfg: Config) -> str:
    return f"temperatura=={cfg.temp_min + cfg.histeresis:g}..{cfg.temp_max - cfg.histeresis:g}"


def cuerpo(descripcion: str, q: str, entidad: str, notify_url: str, token: str = "") -> dict:
    # el token protege /data (sin el, cualquiera con acceso al puerto 5050 podria inventar alertas).
    # Orion prohibe "?" en http.url, por eso con token se usa httpCustom.qs
    destino = {"httpCustom": {"url": notify_url, "qs": {"token": token}}} if token else {"http": {"url": notify_url}}
    # Una suscripcion tiene dos partes: subject (QUE vigilar y CUANDO avisar) y notification (A DONDE y QUE mandar)
    return {
        "description": descripcion,
        "subject": {
            "entities": [{"id": entidad, "type": "Plant"}],   # solo la entidad de la planta
            # entityUpdate: notifica cada trama aunque repita el valor (por defecto Orion solo notifica cambios,
            # y hoy solo funciona porque el IoT Agent cambia el TimeInstant); sin eso la racha no se confirma
            "condition": {"attrs": ["temperatura"], "expression": {"q": q},
                          "alterationTypes": ["entityUpdate", "entityCreate"]},
        },
        "notification": {
            **destino,                                 # POST a http://alertas:5050/data?token=...
            "attrs": ["temperatura", "TimeInstant"],   # solo lo que necesita la maquina de estados
            "attrsFormat": "normalized",               # formato {"value": ..., "type": ..., "metadata": ...}
        },
    }


def sincronizar(orion_url: str, notify_url: str, entidad: str, cfg: Config, token: str = "") -> list[str]:
    # "upsert" por descripcion: si ya existe se hace PATCH (p. ej. cambiaron los umbrales), si no, POST
    r = requests.get(f"{orion_url}/v2/subscriptions", params={"limit": 1000}, headers=CABECERAS, timeout=10)
    r.raise_for_status()
    existentes = {s.get("description"): s["id"] for s in r.json()}
    resultado = []
    for desc, q in ((DESC_FUERA, q_fuera(cfg)), (DESC_DENTRO, q_dentro(cfg))):
        body = cuerpo(desc, q, entidad, notify_url, token)
        if desc in existentes:
            r = requests.patch(f"{orion_url}/v2/subscriptions/{existentes[desc]}", json=body, headers=CABECERAS, timeout=10)
            accion = "actualizada"
        else:
            r = requests.post(f"{orion_url}/v2/subscriptions", json=body, headers=CABECERAS, timeout=10)
            accion = "creada"
        r.raise_for_status()
        resultado.append(f"{accion}: {desc} (q={q})")
    return resultado


if __name__ == "__main__":
    if not os.environ.get("ALERTAS_TOKEN"):
        raise SystemExit("ALERTAS_TOKEN vacio: definirlo en nube/.env (el servicio rechaza /data sin token)")
    for linea in sincronizar(os.environ.get("ORION_URL", "http://orion:1026"),
                             os.environ.get("NOTIFY_URL", "http://alertas:5050/data"),
                             os.environ.get("ENTIDAD", "Plant:plant001"), Config.desde_env(), os.environ.get("ALERTAS_TOKEN", "")):
        print(linea)
