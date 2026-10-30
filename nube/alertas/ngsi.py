# Traductor: convierte el JSON de Orion (NGSI-v2) en una Lectura simple. Es el unico archivo que conoce
# ese formato; el resto del servicio trabaja con Lectura(entity_id, valor, ts).
from datetime import datetime
from typing import NamedTuple


# ts = momento de la medicion en ms epoch (sale del TimeInstant que pone el IoT Agent, no de la hora de llegada)
class Lectura(NamedTuple):
    entity_id: str
    valor: float | None
    ts: int


def iso_a_ms(s: str) -> int:
    # Python 3.10 no acepta la "Z" final en fromisoformat
    return round(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _valor(v) -> float | None:
    # el IoT Agent declara el tipo "Text" aunque el valor viaje como numero JSON
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def leer_entidad(ent: dict, ahora: int) -> Lectura:
    # ent llega asi:
    #   {"id": "Plant:plant001", "type": "Plant",
    #    "temperatura": {"value": 25.3, "type": "Text", "metadata": {"TimeInstant": {...}}},
    #    "TimeInstant": {"value": "2026-10-30T15:00:00.000Z", ...}}
    # El TimeInstant se busca primero en la entidad y si no esta, en la metadata del atributo
    temp = ent.get("temperatura") or {}
    iso = (ent.get("TimeInstant") or {}).get("value") \
        or ((temp.get("metadata") or {}).get("TimeInstant") or {}).get("value")
    # una hora en el futuro (el IoT Agent acepta ?t=) dejaria ultimo_ts adelantado y bloquearia las lecturas siguientes
    return Lectura(ent["id"], _valor(temp.get("value")), min(iso_a_ms(iso), ahora) if iso else ahora)


def leer_notificacion(payload: dict, ahora: int) -> list[Lectura]:
    # una notificacion trae {"subscriptionId": "...", "data": [entidad, ...]}; normalmente una sola entidad
    return [leer_entidad(ent, ahora) for ent in payload.get("data", [])]
