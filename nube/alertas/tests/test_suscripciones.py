from reglas import Config
from suscripciones import DESC_FUERA, cuerpo, q_dentro, q_fuera


def test_q_por_defecto():
    assert q_fuera(Config()) == "temperatura!=10..30;temperatura==0..60"
    assert q_dentro(Config()) == "temperatura==12..28"


def test_q_decimales():
    c = Config(temp_max=32.5)
    assert q_fuera(c) == "temperatura!=10..32.5;temperatura==0..60"
    assert q_dentro(c) == "temperatura==12..30.5"


def test_cuerpo():
    c = cuerpo(DESC_FUERA, "temperatura!=10..30", "Plant:plant001", "http://alertas:5050/data")
    assert c == {
        "description": "alertas: temperatura fuera de rango",
        "subject": {
            "entities": [{"id": "Plant:plant001", "type": "Plant"}],
            "condition": {"attrs": ["temperatura"], "expression": {"q": "temperatura!=10..30"},
                          "alterationTypes": ["entityUpdate", "entityCreate"]},
        },
        "notification": {
            "http": {"url": "http://alertas:5050/data"},
            "attrs": ["temperatura", "TimeInstant"],
            "attrsFormat": "normalized",
        },
    }


def test_cuerpo_con_token():
    # Orion prohibe "?" en http.url: el token va en httpCustom.qs
    n = cuerpo(DESC_FUERA, "q", "Plant:plant001", "http://alertas:5050/data", "abc")["notification"]
    assert "http" not in n
    assert n["httpCustom"] == {"url": "http://alertas:5050/data", "qs": {"token": "abc"}}
    assert n["attrsFormat"] == "normalized"
