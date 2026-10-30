# Persistencia en PostgreSQL (la misma BD del ETL). Dos tablas:
#   alertas_estado  -> una fila por entidad con el Estado actual (sobrevive a reinicios del contenedor)
#   alertas_eventos -> una fila por Transicion; funciona como cola de correos (enviado = false = pendiente)
# El orden de los campos coincide con el de los dataclass, por eso Estado(*fila) arma el objeto directo.
import psycopg2

from reglas import Estado, Transicion

CAMPOS_ESTADO = "entity_id, estado, desde, previo, racha, ultimo_ts, valor, umbral"
CAMPOS_EVENTO = "entity_id, de, a, valor, umbral, ts, ultima_trama"


def conectar(dsn: str):
    return psycopg2.connect(dsn)


def crear_tablas(conn) -> None:
    # "with conn" abre una transaccion: commit al salir bien, rollback si hay excepcion
    with conn, conn.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS alertas_estado (
                entity_id VARCHAR(50) PRIMARY KEY,
                estado VARCHAR(20),
                desde BIGINT,
                previo VARCHAR(20),
                racha INT,
                ultimo_ts BIGINT,
                valor FLOAT,
                umbral TEXT
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS alertas_eventos (
                id SERIAL PRIMARY KEY,
                entity_id VARCHAR(50),
                de VARCHAR(20),
                a VARCHAR(20),
                valor FLOAT,
                umbral TEXT,
                ts BIGINT,
                ultima_trama BIGINT,
                enviado BOOLEAN DEFAULT false,
                enviado_en BIGINT
            );
        """)


def cargar_estado(conn, entity_id: str) -> Estado | None:
    with conn, conn.cursor() as c:
        c.execute(f"SELECT {CAMPOS_ESTADO} FROM alertas_estado WHERE entity_id = %s;", (entity_id,))
        fila = c.fetchone()
    return Estado(*fila) if fila else None


def guardar(conn, e: Estado, t: Transicion | None) -> None:
    # estado y evento en una sola transaccion: no queda un correo sin su estado ni al reves
    with conn, conn.cursor() as c:
        # upsert: inserta la primera vez y despues actualiza la misma fila (EXCLUDED = los valores nuevos)
        c.execute(f"""
            INSERT INTO alertas_estado ({CAMPOS_ESTADO}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (entity_id) DO UPDATE SET
                estado = EXCLUDED.estado, desde = EXCLUDED.desde, previo = EXCLUDED.previo,
                racha = EXCLUDED.racha, ultimo_ts = EXCLUDED.ultimo_ts, valor = EXCLUDED.valor,
                umbral = EXCLUDED.umbral;
        """, (e.entity_id, e.estado, e.desde, e.previo, e.racha, e.ultimo_ts, e.valor, e.umbral))
        if t:
            c.execute(f"INSERT INTO alertas_eventos ({CAMPOS_EVENTO}) VALUES (%s, %s, %s, %s, %s, %s, %s);",
                      (t.entity_id, t.de, t.a, t.valor, t.umbral, t.ts, t.ultima_trama))


def pendientes(conn) -> list[tuple[int, Transicion]]:
    # correos por enviar, del mas viejo al mas nuevo (el cartero los manda en orden)
    with conn, conn.cursor() as c:
        c.execute(f"SELECT id, {CAMPOS_EVENTO} FROM alertas_eventos WHERE NOT enviado ORDER BY id;")
        return [(fila[0], Transicion(*fila[1:])) for fila in c.fetchall()]


def marcar_enviado(conn, evento_id: int, ahora: int) -> None:
    with conn, conn.cursor() as c:
        c.execute("UPDATE alertas_eventos SET enviado = true, enviado_en = %s WHERE id = %s;", (ahora, evento_id))
