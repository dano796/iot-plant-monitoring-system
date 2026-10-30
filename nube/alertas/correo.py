# Arma y envia el correo de una Transicion. asunto() y cuerpo() son texto puro (se prueban sin red);
# enviar() es lo unico que habla con el servidor SMTP. Solo lo usa el hilo cartero de main.py.
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from reglas import OK, ALARMA, Config, Transicion


@dataclass(frozen=True)
class Smtp:
    host: str
    port: int
    user: str
    password: str
    destino: list[str]

    @staticmethod
    def desde_env(env=os.environ) -> "Smtp":
        # ALERTA_DESTINO admite varios correos separados por coma.
        # Se usa "or" y no el default de get() porque compose pasa la variable vacia ("") cuando no esta en .env
        destino = [d.strip() for d in env.get("ALERTA_DESTINO", "").split(",") if d.strip()]
        return Smtp(env.get("SMTP_HOST") or "smtp.gmail.com", int(env.get("SMTP_PORT") or 465),
                    env.get("SMTP_USER", ""), env.get("SMTP_PASS", ""), destino)


def nombre_alarma(entity_id: str) -> str:
    return "temperatura-" + entity_id.split(":")[-1]


def _hora(ms: int, zona: ZoneInfo, fmt: str = "%H:%M:%S") -> str:
    return datetime.fromtimestamp(ms / 1000, zona).strftime(fmt)


def asunto(t: Transicion, zona: ZoneInfo) -> str:
    # ej.: "ALARMA: temperatura-plant001 en ALARMA (33.0 °C > 30 °C)"
    nombre = nombre_alarma(t.entity_id)
    if t.a == ALARMA:
        return f"ALARMA: {nombre} en ALARMA ({t.valor:.1f} °C {t.umbral} °C)"
    if t.a == OK:
        return f"OK: {nombre} volvió a OK ({t.valor:.1f} °C)"
    return f"SIN DATOS: {nombre} sin datos desde {_hora(t.ultima_trama, zona, '%H:%M')}"


def cuerpo(t: Transicion, cfg: Config, front_url: str, zona: ZoneInfo) -> str:
    if t.valor is None:
        valor = f"sin datos desde {_hora(t.ultima_trama, zona)} (más de {cfg.sin_datos_s} s sin tramas)"
    else:
        valor = f"{t.valor:.1f} °C" + (f" ({t.umbral} °C)" if t.a == ALARMA else "")
    lineas = [
        f"{nombre_alarma(t.entity_id)}: {t.de} → {t.a}",
        "",
        f"Valor: {valor}",
        f"Hora: {_hora(t.ts, zona, '%Y-%m-%d %H:%M:%S')}",
        f"Regla: entra en ALARMA fuera de {cfg.temp_min:g}–{cfg.temp_max:g} °C; "
        f"vuelve a OK dentro de {cfg.temp_min + cfg.histeresis:g}–{cfg.temp_max - cfg.histeresis:g} °C "
        f"({cfg.confirmaciones} lecturas seguidas).",
    ]
    if front_url:
        lineas += ["", f"Dashboard: {front_url}"]
    return "\n".join(lineas)


def enviar(asunto: str, cuerpo: str, smtp: Smtp) -> bool:
    # devuelve False en vez de lanzar: el cartero usa eso para dejar el evento pendiente y reintentar
    if not smtp.destino:
        print("Correo no enviado: ALERTA_DESTINO vacío")
        return False
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = smtp.user or "alertas@localhost"
    msg["To"] = ", ".join(smtp.destino)
    msg.set_content(cuerpo)
    try:
        # 465 = Gmail con SSL; otro puerto = SMTP plano (mailpit en pruebas locales)
        cliente = smtplib.SMTP_SSL if smtp.port == 465 else smtplib.SMTP
        with cliente(smtp.host, smtp.port, timeout=10) as s:
            if smtp.user:
                s.login(smtp.user, smtp.password)
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"Error enviando correo: {e}")
        return False
