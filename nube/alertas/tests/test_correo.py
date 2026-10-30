from datetime import datetime
from zoneinfo import ZoneInfo

from correo import Smtp, asunto, cuerpo, enviar, nombre_alarma
from reglas import OK, ALARMA, SIN_DATOS, Config, Transicion

zona = ZoneInfo("America/Bogota")
ID = "Plant:plant001"


def ms(h, m, s=0):
    return int(datetime(2026, 10, 21, h, m, s, tzinfo=zona).timestamp() * 1000)


def test_nombre_alarma():
    assert nombre_alarma(ID) == "temperatura-plant001"


def test_asunto_alarma():
    t = Transicion(ID, OK, ALARMA, 36.4, "> 30", ms(14, 32, 10), ms(14, 32, 10))
    assert asunto(t, zona) == "ALARMA: temperatura-plant001 en ALARMA (36.4 °C > 30 °C)"


def test_asunto_ok():
    t = Transicion(ID, ALARMA, OK, 27.1, "12–28", ms(15, 0), ms(15, 0))
    assert asunto(t, zona) == "OK: temperatura-plant001 volvió a OK (27.1 °C)"


def test_asunto_sin_datos():
    t = Transicion(ID, OK, SIN_DATOS, None, None, ms(14, 34, 5), ms(14, 32))
    assert asunto(t, zona) == "SIN DATOS: temperatura-plant001 sin datos desde 14:32"


def test_cuerpo_contiene():
    t = Transicion(ID, OK, ALARMA, 36.4, "> 30", ms(14, 32, 10), ms(14, 32, 10))
    c = cuerpo(t, Config(), "http://front.local", zona)
    for texto in ["OK → ALARMA", "36.4 °C", "fuera de 10–30 °C", "dentro de 12–28 °C", "14:32:10", "http://front.local"]:
        assert texto in c


def test_smtp_desde_env():
    s = Smtp.desde_env({"ALERTA_DESTINO": "a@x.com, b@y.com"})
    assert s.destino == ["a@x.com", "b@y.com"]
    assert (s.host, s.port) == ("smtp.gmail.com", 465)
    assert Smtp.desde_env({}).destino == []


def test_enviar_sin_destino_no_falla():
    assert enviar("a", "b", Smtp("localhost", 1, "", "", [])) is False
