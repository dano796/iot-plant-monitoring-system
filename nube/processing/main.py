# ETL: pasa las lecturas crudas de CrateDB (historico de QuantumLeap) a PostgreSQL, limpias y clasificadas.
# Corre en un ciclo cada PERIODO_S segundos (60 por defecto). Cada vuelta:
#   1. Extract   -> lee de CrateDB solo las filas posteriores al ultimo timestamp guardado en Postgres
#   2. Transform -> convierte a numero, descarta fuera de rango y clasifica segun la humedad de suelo
#   3. Load      -> inserta en la tabla mediciones_limpias (la consulta la API)
import os
import time
import psycopg2
from crate import client
import pandas as pd

def process_data():
    print("--- Ejecutando ciclo de Limpieza y Analítica ---")
    try:
        # 1. Conexión a CrateDB
        crate_conn = client.connect("http://cratedb:4200")
        crate_cursor = crate_conn.cursor()

        # Postgres primero: el ultimo timestamp guardado marca desde donde procesar (evita duplicados)
        pg_conn = psycopg2.connect(
            host="postgres",
            port=5432,
            dbname="plantas_db",
            user="admin",
            password="adminpassword"
        )
        pg_cursor = pg_conn.cursor()
        pg_cursor.execute("""
            CREATE TABLE IF NOT EXISTS mediciones_limpias (
                id SERIAL PRIMARY KEY,
                entity_id VARCHAR(50),
                timestamp BIGINT,
                temperatura FLOAT,
                humedad_aire FLOAT,
                humedad_suelo FLOAT,
                luminosidad FLOAT,
                estado_planta VARCHAR(30)
            );
        """)
        pg_cursor.execute("SELECT COALESCE(MAX(timestamp), 0) FROM mediciones_limpias;")
        ultimo = pg_cursor.fetchone()[0]

        # 2. Leer datos crudos NUEVOS de CrateDB (soportando nombres viejos y nuevos)
        #    mtopeniot.etplant = tabla que crea QuantumLeap: esquema mt<servicio>, tabla et<tipo de entidad>
        query = "SELECT entity_id, timeinstant, temperatura, humedad, humedadsuelo, luz FROM mtopeniot.etplant WHERE timeinstant > ? ORDER BY timeinstant;"
        try:
            crate_cursor.execute(query, (ultimo,))
            rows = crate_cursor.fetchall()
        except Exception:
            # Fallback por si hay registros viejos con nombres antiguos
            query = "SELECT entity_id, timeinstant, temp, h_aire, h_suelo, luz FROM mtopeniot.etplant WHERE timeinstant > ? ORDER BY timeinstant;"
            crate_cursor.execute(query, (ultimo,))
            rows = crate_cursor.fetchall()

        if not rows:
            print("Sin registros nuevos en CrateDB.")
            pg_conn.close()
            return

        df = pd.DataFrame(rows, columns=['entity_id', 'timeinstant', 'temp', 'h_aire', 'h_suelo', 'luz'])

        # --- LIMPIEZA DE DATOS ---
        # el IoT Agent guarda los valores como texto: lo que no se pueda convertir queda NaN
        df['temp'] = pd.to_numeric(df['temp'], errors='coerce')
        df['h_aire'] = pd.to_numeric(df['h_aire'], errors='coerce')
        df['h_suelo'] = pd.to_numeric(df['h_suelo'], errors='coerce')
        df['luz'] = pd.to_numeric(df['luz'], errors='coerce')

        # Filtrar valores atípicos (outliers/rangos válidos). NaN no cumple ninguna comparacion: tambien se descarta
        df_clean = df[
            (df['temp'] >= 0) & (df['temp'] <= 60) &
            (df['h_suelo'] >= 0) & (df['h_suelo'] <= 100) &
            (df['h_aire'] >= 0) & (df['h_aire'] <= 100)
        ].copy()

        # --- ANALÍTICA DE DATOS ---
        def evaluar_estado(suelo):
            if suelo < 30:
                return "NECESITA_RIEGO"
            elif suelo > 80:
                return "EXCESO_AGUA"
            else:
                return "OPTIMO"

        df_clean['estado_planta'] = df_clean['h_suelo'].apply(evaluar_estado)

        # 3. Guardar en PostgreSQL
        for _, row in df_clean.iterrows():
            pg_cursor.execute("""
                INSERT INTO mediciones_limpias 
                (entity_id, timestamp, temperatura, humedad_aire, humedad_suelo, luminosidad, estado_planta)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (
                row['entity_id'],
                int(row['timeinstant']),
                float(row['temp']),
                float(row['h_aire']),
                float(row['h_suelo']),
                float(row['luz']),
                row['estado_planta']
            ))

        pg_conn.commit()
        pg_cursor.close()
        pg_conn.close()
        print(f"Éxito: {len(df_clean)} registros limpios insertados en PostgreSQL.")

    except Exception as e:
        # se registra y se reintenta en el siguiente ciclo (CrateDB o Postgres pueden no estar listos aun)
        print(f"Error en procesamiento: {e}")

if __name__ == "__main__":
    while True:   # ciclo fijo; si hace falta tiempo real, suscribir este modulo a Orion
        process_data()
        time.sleep(int(os.environ.get("PERIODO_S", "60")))
