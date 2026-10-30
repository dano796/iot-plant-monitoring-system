import copy

from ngsi import Lectura, iso_a_ms, leer_entidad, leer_notificacion

TS = "2026-10-16T19:22:16.831Z"
MS = 1792178536831
AHORA = 1800000000000

# entidad de ARQUITECTURA.md §2.3 (recortada a lo que llega en la notificacion)
FIXTURE = {
    "id": "Plant:plant001",
    "type": "Plant",
    "temperatura": {"type": "Text", "value": 24.936523, "metadata": {"TimeInstant": {"type": "DateTime", "value": TS}}},
    "TimeInstant": {"type": "DateTime", "value": TS},
}


def con_valor(v):
    ent = copy.deepcopy(FIXTURE)
    if v is None:
        del ent["temperatura"]
    else:
        ent["temperatura"]["value"] = v
    return ent


def test_iso_a_ms():
    assert iso_a_ms(TS) == MS


def test_notificacion_basica():
    payload = {"subscriptionId": "x", "data": [FIXTURE]}
    assert leer_notificacion(payload, AHORA) == [Lectura("Plant:plant001", 24.936523, MS)]


def test_valor_texto_y_basura():
    assert leer_entidad(con_valor("36.4"), AHORA).valor == 36.4
    assert leer_entidad(con_valor("abc"), AHORA).valor is None
    assert leer_entidad(con_valor(True), AHORA).valor is None
    assert leer_entidad(con_valor(None), AHORA).valor is None


def test_sin_timeinstant_usa_ahora():
    ent = copy.deepcopy(FIXTURE)
    del ent["TimeInstant"]
    del ent["temperatura"]["metadata"]
    assert leer_entidad(ent, AHORA).ts == AHORA


def test_timeinstant_de_metadata():
    ent = copy.deepcopy(FIXTURE)
    del ent["TimeInstant"]
    ent["temperatura"]["metadata"]["TimeInstant"]["value"] = "2026-10-16T19:22:17.000Z"
    assert leer_entidad(ent, AHORA).ts == MS + 169


def test_varias_entidades():
    otra = copy.deepcopy(FIXTURE)
    otra["id"] = "Plant:falso999"
    lecturas = leer_notificacion({"subscriptionId": "x", "data": [FIXTURE, otra]}, AHORA)
    assert [l.entity_id for l in lecturas] == ["Plant:plant001", "Plant:falso999"]


def test_timeinstant_futuro_se_limita_a_ahora():
    # el IoT Agent acepta ?t= : una trama fechada en el futuro no debe bloquear las lecturas siguientes
    ent = copy.deepcopy(FIXTURE)
    ent["TimeInstant"]["value"] = "2099-01-01T00:00:00.000Z"
    assert leer_entidad(ent, AHORA).ts == AHORA
