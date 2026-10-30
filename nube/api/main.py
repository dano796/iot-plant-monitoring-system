# API de lectura del dashboard. Solo consulta la tabla mediciones_limpias que llena el ETL (processing/).
#   GET /api/plantas/ultimo     -> la ultima medicion limpia
#   GET /api/plantas/historico  -> las ultimas 50
#   GET /api/plantas/rango      -> promedios por ventana entre dos instantes (pestaña de historicos)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI(title="API Plant Monitoring System")

# Habilitar CORS para permitir llamadas desde el Frontend Dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# una conexion nueva por peticion (mismas credenciales que en docker-compose.yml)
def get_db_connection():
    return psycopg2.connect(
        host="postgres",
        port=5432,
        dbname="plantas_db",
        user="admin",
        password="adminpassword"
    )

@app.get("/")
def read_root():
    return {"message": "API Backend del Sistema de Monitoreo de Plantas activa"}

@app.get("/api/plantas/ultimo")
def get_ultima_medicion():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM mediciones_limpias ORDER BY id DESC LIMIT 1;")
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="No se encontraron datos registrados")
        return row
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/plantas/historico")
def get_historico():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM mediciones_limpias ORDER BY id DESC LIMIT 50;")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/plantas/rango")
def get_rango(desde: int, hasta: int, puntos: int = 500):
    # desde/hasta en ms epoch; promedia en ventanas para devolver ~`puntos` filas
    # (ventanas de minimo 10 s y como maximo 2000 puntos, para no mandar miles de lecturas al grafico)
    if hasta <= desde:
        raise HTTPException(status_code=400, detail="hasta debe ser mayor que desde")
    ventana = max(10000, (hasta - desde) // max(1, min(puntos, 2000)))
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT (timestamp / %(v)s) * %(v)s AS timestamp,
                   ROUND(AVG(temperatura)::numeric, 2)::float AS temperatura,
                   ROUND(AVG(humedad_aire)::numeric, 2)::float AS humedad_aire,
                   ROUND(AVG(humedad_suelo)::numeric, 2)::float AS humedad_suelo,
                   ROUND(AVG(luminosidad)::numeric, 2)::float AS luminosidad,
                   COUNT(*) AS lecturas
            FROM mediciones_limpias
            WHERE timestamp BETWEEN %(d)s AND %(h)s
            GROUP BY 1 ORDER BY 1;
        """, {"v": ventana, "d": desde, "h": hasta})
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
