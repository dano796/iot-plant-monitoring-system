# Maquina de estados de la alarma. Es logica pura: no toca red, BD ni reloj (la hora llega por parametro),
# por eso se prueba sola en tests/test_reglas.py. Cada funcion recibe un Estado y devuelve
# (nuevo_estado, transicion | None); transicion != None significa "cambio de estado -> hay que avisar".
import os
from dataclasses import dataclass, replace
from typing import Mapping

# Estados de la alarma (como CloudWatch: OK / ALARM / INSUFFICIENT_DATA)
OK, ALARMA, SIN_DATOS = "OK", "ALARMA", "SIN_DATOS"
# Clases de una lectura. Con los valores por defecto:
#   0      10    12               28    30      60
#   |FUERA |BANDA|     DENTRO     |BANDA| FUERA |   (fuera de 0-60 = INVALIDA, sensor roto)
FUERA, DENTRO, BANDA, INVALIDA = "FUERA", "DENTRO", "BANDA", "INVALIDA"


# frozen=True en las tres clases: son inmutables, para "cambiar" algo se crea una copia con replace()
@dataclass(frozen=True)
class Config:
    temp_min: float = 10
    temp_max: float = 30
    histeresis: float = 2
    confirmaciones: int = 3
    sin_datos_s: int = 120
    valido_min: float = 0      # fuera de [valido_min, valido_max] la lectura se ignora (mismo filtro que el ETL)
    valido_max: float = 60

    def __post_init__(self):
        # sin esto un umbral mal puesto falla en silencio: con la banda DENTRO vacia la ALARMA nunca vuelve a OK
        if self.histeresis < 0 or self.temp_min + self.histeresis >= self.temp_max - self.histeresis:
            raise ValueError(f"HISTERESIS={self.histeresis:g} deja vacio el rango de vuelta a OK "
                             f"({self.temp_min:g}–{self.temp_max:g} °C)")
        if self.temp_min < self.valido_min or self.temp_max > self.valido_max:
            raise ValueError(f"TEMP_MIN–TEMP_MAX ({self.temp_min:g}–{self.temp_max:g}) debe estar dentro de "
                             f"VALIDO_MIN–VALIDO_MAX ({self.valido_min:g}–{self.valido_max:g})")
        if self.confirmaciones < 1:
            raise ValueError(f"CONFIRMACIONES={self.confirmaciones} debe ser al menos 1")

    @staticmethod
    def desde_env(env: Mapping[str, str] = os.environ) -> "Config":
        d = Config()
        f = lambda k, v: float(env.get(k, v))
        i = lambda k, v: int(env.get(k, v))
        return Config(f("TEMP_MIN", d.temp_min), f("TEMP_MAX", d.temp_max), f("HISTERESIS", d.histeresis),
                      i("CONFIRMACIONES", d.confirmaciones), i("SIN_DATOS_S", d.sin_datos_s),
                      f("VALIDO_MIN", d.valido_min), f("VALIDO_MAX", d.valido_max))


# Lo que se guarda en la tabla alertas_estado (una fila por entidad): la "memoria" de la alarma
@dataclass(frozen=True)
class Estado:
    entity_id: str
    estado: str
    desde: int                     # ms epoch del ultimo cambio de estado
    previo: str | None = None      # estado antes de SIN_DATOS
    racha: int = 0                 # lecturas seguidas contrarias al estado actual
    ultimo_ts: int = 0             # TimeInstant de la ultima lectura valida procesada
    valor: float | None = None
    umbral: str | None = None


# Un cambio de estado (p. ej. OK -> ALARMA). Se guarda en alertas_eventos y de ahi sale el correo
@dataclass(frozen=True)
class Transicion:
    entity_id: str
    de: str
    a: str
    valor: float | None
    umbral: str | None
    ts: int
    ultima_trama: int              # para el correo de SIN_DATOS: "sin datos desde las HH:MM"


def clasificar(valor: float | None, cfg: Config) -> str:
    if valor is None or not cfg.valido_min <= valor <= cfg.valido_max:
        return INVALIDA
    if valor < cfg.temp_min or valor > cfg.temp_max:
        return FUERA
    if cfg.temp_min + cfg.histeresis <= valor <= cfg.temp_max - cfg.histeresis:
        return DENTRO
    return BANDA


def estado_inicial(entity_id: str, ahora: int) -> Estado:
    return Estado(entity_id, OK, ahora)


# Texto del umbral para el correo y el panel: "12–28" al volver a OK, "> 30" o "< 10" al entrar en ALARMA
def _umbral(a: str, valor: float | None, cfg: Config) -> str | None:
    if a == OK:
        return f"{cfg.temp_min + cfg.histeresis:g}–{cfg.temp_max - cfg.histeresis:g}"
    if a == ALARMA:
        # alta o baja segun el lado del rango (tambien vale para un 29 que sale de SIN_DATOS a ALARMA)
        alta = valor >= (cfg.temp_min + cfg.temp_max) / 2
        return f"> {cfg.temp_max:g}" if alta else f"< {cfg.temp_min:g}"
    return None


def _transicion(e: Estado, a: str, valor: float | None, ts: int, ultima_trama: int, cfg: Config):
    # cambia al estado "a": reinicia la racha y recuerda el estado previo solo si se entra a SIN_DATOS
    umbral = _umbral(a, valor, cfg)
    nuevo = replace(e, estado=a, desde=ts, previo=e.estado if a == SIN_DATOS else None,
                    racha=0, valor=valor, umbral=umbral)
    return nuevo, Transicion(e.entity_id, e.estado, a, valor, umbral, ts, ultima_trama)


def aplicar_lectura(e: Estado, valor: float | None, ts: int, cfg: Config) -> tuple[Estado, Transicion | None]:
    # Se llama con cada notificacion de Orion (/data). Pasos:
    #   1. descartar duplicadas/atrasadas e invalidas
    #   2. si estaba en SIN_DATOS, salir de inmediato
    #   3. si no, contar lecturas "contrarias" seguidas (la racha) y cambiar al llegar a CONFIRMACIONES
    if ts <= e.ultimo_ts:          # notificacion repetida o atrasada: idempotencia
        return e, None
    clase = clasificar(valor, cfg)
    if clase == INVALIDA:
        return e, None
    # valor y umbral quedan los de la ultima transicion; si no, el panel mostraria "ALARMA 25.0 °C > 30 °C"
    e = replace(e, ultimo_ts=ts)

    if e.estado == SIN_DATOS:      # vuelven los datos: directo al estado de la lectura, sin confirmar
        destino = {FUERA: ALARMA, DENTRO: OK}.get(clase, e.previo or OK)
        return _transicion(e, destino, valor, ts, ts, cfg)

    # en OK lo que empuja a cambiar es una lectura FUERA; en ALARMA, una DENTRO.
    # Ej. en OK: 31, 32, 33 -> ALARMA (3 seguidas). 31, 25, 32 -> el 25 reinicia la racha, no hay alarma
    contraria = FUERA if e.estado == OK else DENTRO
    if clase == contraria:
        if e.racha + 1 >= cfg.confirmaciones:
            return _transicion(e, ALARMA if e.estado == OK else OK, valor, ts, ts, cfg)
        return replace(e, racha=e.racha + 1), None
    if clase == BANDA:             # histeresis: ni suma ni reinicia
        return e, None
    return replace(e, racha=0), None   # lectura "a favor" del estado actual: la racha vuelve a 0


def revisar_periodica(e: Estado, valor: float | None, ts: int, ahora: int,
                      cfg: Config) -> tuple[Estado, Transicion | None]:
    # Se llama cada REVISION_S desde el hilo "revisar" de main.py. Detecta el silencio, que ninguna
    # suscripcion puede detectar (Orion solo avisa cuando llega algo, no cuando deja de llegar)
    # ts = TimeInstant actual de la entidad en Orion (se actualiza con cada trama, aunque no haya notificacion)
    if ahora - ts > cfg.sin_datos_s * 1000:
        if e.estado == SIN_DATOS:
            return e, None
        return _transicion(e, SIN_DATOS, None, ahora, ts, cfg)
    if e.estado == SIN_DATOS:      # volvio una trama que ninguna suscripcion notifico (banda)
        return aplicar_lectura(e, valor, ts, cfg)
    return e, None
