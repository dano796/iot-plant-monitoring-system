from dataclasses import replace

import pytest
import requests

import main
from test_ngsi import FIXTURE, MS
from reglas import OK, ALARMA, SIN_DATOS, Transicion, estado_inicial


@pytest.fixture
def cliente(monkeypatch):
    llamadas = []
    monkeypatch.setattr(main, "actualizar", lambda entity_id, paso: llamadas.append(entity_id))
    monkeypatch.setattr(main, "TOKEN", "secreto")
    return main.app.test_client(), llamadas


def payload(*ids):
    return {"subscriptionId": "x", "data": [dict(FIXTURE, id=i) for i in ids]}


def test_data_sin_token_403(cliente):
    c, llamadas = cliente
    assert c.post("/data", json=payload("Plant:plant001")).status_code == 403
    assert c.post("/data?token=otro", json=payload("Plant:plant001")).status_code == 403
    assert llamadas == []


def test_data_con_token(cliente):
    c, llamadas = cliente
    assert c.post("/data?token=secreto", json=payload("Plant:plant001")).status_code == 204
    assert llamadas == ["Plant:plant001"]


def test_data_otra_entidad_se_ignora(cliente):
    c, llamadas = cliente
    assert c.post("/data?token=secreto", json=payload("Plant:falso999", "Plant:plant001")).status_code == 204
    assert llamadas == ["Plant:plant001"]


def test_pendiente_imposible_de_armar_no_bloquea(monkeypatch):
    ahora = main.ahora_ms()
    malo = Transicion("Plant:plant001", OK, ALARMA, None, "> 30", ahora, ahora)       # valor None: asunto() falla
    bueno = Transicion("Plant:plant001", OK, ALARMA, 36.0, "> 30", ahora, ahora)
    marcados, enviados = cola(monkeypatch, [(1, malo), (2, bueno)], main.Smtp("h", 465, "", "", ["a@x.com"]))
    main.enviar_pendientes()
    assert marcados == [1, 2]
    assert len(enviados) == 1


def cola(monkeypatch, eventos, smtp):
    marcados, enviados = [], []

    class Conn:
        def close(self): pass

    monkeypatch.setattr(main.almacen, "conectar", lambda dsn: Conn())
    monkeypatch.setattr(main.almacen, "pendientes", lambda conn: eventos)
    monkeypatch.setattr(main.almacen, "marcar_enviado", lambda conn, i, ahora: marcados.append(i))
    monkeypatch.setattr(main, "enviar", lambda a, c, s: enviados.append(a) or True)
    monkeypatch.setattr(main, "SMTP", smtp)
    return marcados, enviados


def test_sin_destino_descarta_pendientes(monkeypatch):
    # sin ALERTA_DESTINO no se acumulan correos que despues saldrian en rafaga
    t = Transicion("Plant:plant001", OK, ALARMA, 36.0, "> 30", main.ahora_ms(), main.ahora_ms())
    marcados, enviados = cola(monkeypatch, [(1, t)], main.Smtp("h", 465, "", "", []))
    main.enviar_pendientes()
    assert (marcados, enviados) == ([1], [])


def test_pendiente_viejo_se_descarta(monkeypatch):
    ahora = main.ahora_ms()
    viejo = Transicion("Plant:plant001", OK, ALARMA, 36.0, "> 30", ahora - 2 * 3600 * 1000, 0)
    nuevo = Transicion("Plant:plant001", ALARMA, OK, 25.0, "12–28", ahora, ahora)
    marcados, enviados = cola(monkeypatch, [(1, viejo), (2, nuevo)], main.Smtp("h", 465, "", "", ["a@x.com"]))
    main.enviar_pendientes()
    assert marcados == [1, 2]
    assert enviados == ["OK: temperatura-plant001 volvió a OK (25.0 °C)"]


def test_data_sin_token_configurado_rechaza(monkeypatch):
    # falla cerrado: sin ALERTAS_TOKEN nadie puede inventar alertas
    llamadas = []
    monkeypatch.setattr(main, "actualizar", lambda entity_id, paso: llamadas.append(entity_id))
    monkeypatch.setattr(main, "TOKEN", "")
    c = main.app.test_client()
    assert c.post("/data", json=payload("Plant:plant001")).status_code == 403
    assert c.post("/data?token=", json=payload("Plant:plant001")).status_code == 403
    assert llamadas == []


def test_data_token_no_ascii_403(cliente):
    c, llamadas = cliente
    assert c.post("/data?token=%C3%B1", json=payload("Plant:plant001")).status_code == 403
    assert llamadas == []


def bd_falsa(monkeypatch):
    guardados = []

    class Conn:
        def close(self): pass

    monkeypatch.setattr(main.almacen, "conectar", lambda dsn: Conn())
    monkeypatch.setattr(main.almacen, "cargar_estado", lambda conn, i: None)
    monkeypatch.setattr(main.almacen, "guardar", lambda conn, e, t: guardados.append((e, t)))
    return guardados


def test_actualizar_no_envia_correos_en_el_request(monkeypatch):
    # /data solo avisa al hilo de correo: Orion no espera al SMTP
    bd_falsa(monkeypatch)
    monkeypatch.setattr(main, "enviar_pendientes", lambda: pytest.fail("envio dentro del request"))
    main.hay_eventos.clear()
    t = Transicion("Plant:plant001", OK, ALARMA, 36.0, "> 30", 1, 1)
    main.actualizar("Plant:plant001", lambda e: (replace(e, estado=ALARMA), t))
    assert main.hay_eventos.is_set()
    assert main.difusion.get_nowait()["estado"] == ALARMA


def test_actualizar_sin_transicion_no_despierta(monkeypatch):
    bd_falsa(monkeypatch)
    main.hay_eventos.clear()
    main.actualizar("Plant:plant001", lambda e: (e, None))
    assert not main.hay_eventos.is_set()


class Respuesta:
    def __init__(self, codigo, cuerpo=None):
        self.status_code, self.cuerpo = codigo, cuerpo

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.cuerpo


@pytest.mark.parametrize("respuesta", [Respuesta(404), requests.ConnectionError("orion caido")])
def test_revision_sin_entidad_pasa_a_sin_datos(monkeypatch, respuesta):
    def get(*a, **kw):
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta
    pasos = []
    monkeypatch.setattr(main.requests, "get", get)
    monkeypatch.setattr(main, "actualizar", lambda entity_id, paso: pasos.append(paso))
    monkeypatch.setattr(main, "ahora_ms", lambda: 1000 + 121000)
    assert main.revisar_una(1000) == 1000
    e, t = pasos[0](estado_inicial("Plant:plant001", 0))
    assert (e.estado, t.a, t.ultima_trama) == (SIN_DATOS, SIN_DATOS, 1000)


def test_revision_recuerda_la_ultima_trama(monkeypatch):
    monkeypatch.setattr(main.requests, "get", lambda *a, **kw: Respuesta(200, FIXTURE))
    monkeypatch.setattr(main, "actualizar", lambda entity_id, paso: None)
    monkeypatch.setattr(main, "ahora_ms", lambda: 2_000_000_000_000)
    assert main.revisar_una(0) == MS
