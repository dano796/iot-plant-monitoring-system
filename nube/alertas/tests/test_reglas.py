import pytest

from reglas import (OK, ALARMA, SIN_DATOS, FUERA, DENTRO, BANDA, INVALIDA,
                    Config, clasificar, estado_inicial, aplicar_lectura, revisar_periodica)

cfg = Config()
ID = "Plant:plant001"


def lecturas(e, valores, t0=1000):
    transiciones = []
    for i, v in enumerate(valores):
        e, t = aplicar_lectura(e, v, t0 + i * 10000, cfg)
        if t:
            transiciones.append(t)
    return e, transiciones


def inicial():
    return estado_inicial(ID, 0)


@pytest.mark.parametrize("v,clase", [(5, FUERA), (0, FUERA), (60, FUERA), (35, FUERA),
    (25, DENTRO), (12, DENTRO), (28, DENTRO), (29, BANDA), (30, BANDA), (10, BANDA), (11, BANDA),
    (125, INVALIDA), (-1, INVALIDA), (None, INVALIDA)])
def test_clasificar(v, clase):
    assert clasificar(v, Config()) == clase


def test_tres_fuera_dispara_alarma():
    e, ts = lecturas(inicial(), [36.4] * 3)
    assert e.estado == ALARMA
    assert len(ts) == 1
    t = ts[0]
    assert (t.de, t.a, t.valor, t.umbral, t.ts) == (OK, ALARMA, 36.4, "> 30", 21000)
    assert (e.valor, e.umbral, e.desde) == (36.4, "> 30", 21000)


def test_alarma_baja():
    e, ts = lecturas(inicial(), [5] * 3)
    assert e.estado == ALARMA
    assert ts[0].umbral == "< 10"


def test_dos_fuera_no_dispara():
    e, ts = lecturas(inicial(), [36, 36])
    assert (e.estado, e.racha, ts) == (OK, 2, [])


def test_lectura_dentro_reinicia_racha():
    e, ts = lecturas(inicial(), [36, 36, 25, 36])
    assert (e.estado, e.racha, ts) == (OK, 1, [])


def test_banda_es_neutra():
    e, ts = lecturas(inicial(), [31, 31, 29, 31])
    assert e.estado == ALARMA
    assert len(ts) == 1
    assert ts[0].ts == 31000


def test_alarma_no_se_repite():
    e, ts = lecturas(inicial(), [36] * 10)
    assert e.estado == ALARMA
    assert len(ts) == 1


def test_vuelve_a_ok():
    e, ts = lecturas(inicial(), [36] * 3 + [25] * 3)
    assert e.estado == OK
    assert len(ts) == 2
    assert (ts[1].de, ts[1].a, ts[1].umbral, ts[1].valor) == (ALARMA, OK, "12–28", 25)


def test_banda_no_cierra_alarma():
    e, ts = lecturas(inicial(), [36] * 3 + [29] * 5)
    assert e.estado == ALARMA
    assert len(ts) == 1


def test_ts_repetido_se_ignora():
    e = inicial()
    for _ in range(3):
        e, t = aplicar_lectura(e, 36, 5000, cfg)
    assert (e.estado, e.racha) == (OK, 1)


def test_ts_viejo_se_ignora():
    e, _ = aplicar_lectura(inicial(), 36, 9000, cfg)
    e2, t = aplicar_lectura(e, 36, 8000, cfg)
    assert e2 == e
    assert t is None


def test_invalida_se_ignora():
    e, ts = lecturas(inicial(), [125] * 3)
    assert (e.estado, e.racha, e.ultimo_ts, ts) == (OK, 0, 0, [])


def test_sin_datos():
    e, t = revisar_periodica(inicial(), 25, 0, 121000, cfg)
    assert (e.estado, e.previo) == (SIN_DATOS, OK)
    assert (t.de, t.a, t.valor, t.umbral, t.ts, t.ultima_trama) == (OK, SIN_DATOS, None, None, 121000, 0)


def test_sin_datos_aun_no():
    e, t = revisar_periodica(inicial(), 25, 0, 119000, cfg)
    assert (e.estado, t) == (OK, None)


def test_sin_datos_no_se_repite():
    e, _ = revisar_periodica(inicial(), 25, 0, 121000, cfg)
    e2, t = revisar_periodica(e, 25, 0, 200000, cfg)
    assert (e2.estado, t) == (SIN_DATOS, None)


def en_sin_datos(previo_valores):
    e, _ = lecturas(inicial(), previo_valores)
    e, _ = revisar_periodica(e, None, e.ultimo_ts, e.ultimo_ts + 121000, cfg)
    assert e.estado == SIN_DATOS
    return e


def test_sale_de_sin_datos_directo():
    e = en_sin_datos([])
    e1, t = aplicar_lectura(e, 36, 500000, cfg)
    assert (e1.estado, t.de, t.a) == (ALARMA, SIN_DATOS, ALARMA)
    e2, t = aplicar_lectura(e, 25, 500000, cfg)
    assert (e2.estado, t.a) == (OK, OK)
    e3, t = aplicar_lectura(en_sin_datos([36] * 3), 29, 500000, cfg)
    assert (e3.estado, t.a) == (ALARMA, ALARMA)


def test_sale_de_sin_datos_por_revision():
    e = en_sin_datos([])
    e2, t = revisar_periodica(e, 29, 500000, 510000, cfg)
    assert e2.estado == OK
    assert (t.de, t.a) == (SIN_DATOS, OK)


def test_config_desde_env():
    c = Config.desde_env({"TEMP_MAX": "32.5", "CONFIRMACIONES": "5"})
    assert (c.temp_max, c.confirmaciones) == (32.5, 5)
    assert (c.temp_min, c.histeresis, c.sin_datos_s, c.valido_min, c.valido_max) == (10, 2, 120, 0, 60)


def test_alarma_conserva_valor_de_la_transicion():
    # una lectura sin transicion no cambia valor: el panel no debe mostrar "25.0 °C > 30 °C"
    e, _ = lecturas(inicial(), [36] * 3 + [25])
    assert (e.estado, e.racha, e.valor, e.umbral) == (ALARMA, 1, 36, "> 30")


@pytest.mark.parametrize("kw", [dict(temp_min=20, temp_max=24, histeresis=3), dict(histeresis=10),
    dict(histeresis=-1), dict(confirmaciones=0), dict(temp_max=70), dict(temp_min=-5)])
def test_config_invalida(kw):
    with pytest.raises(ValueError):
        Config(**kw)
