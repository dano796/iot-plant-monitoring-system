# Sistema de monitoreo de plantas IoT

Un nodo LilyGO T-Beam mide temperatura, humedad del aire, humedad del suelo, luz y posición GPS de una planta, y envía cada 10 s una trama UltraLight 2.0 a una nube basada en FIWARE. La nube guarda el histórico, limpia y clasifica los datos, los muestra en un dashboard web y envía alertas por correo y websocket cuando la temperatura sale de rango.

```
T-Beam ──HTTP UltraLight──► IoT Agent :5555 ──► Orion :1026 ──► QuantumLeap :8668 ──► CrateDB (histórico crudo)
                                                  │                                        │ cada 60 s
                                                  │ suscripciones                          ▼
                                                  ▼                              processing (ETL) ──► PostgreSQL
                                        alertas :5050 ──► correo                                       │
                                                  └──── websocket ──► frontend :80 ◄── api :8000 ◄─────┘
```

## Estructura

```
firmware/                 Proyecto PlatformIO del nodo sensor (ESP32 / T-Beam V1.1)
  platformio.ini
  src/main.cpp            Máquina de estados CHIRP → PRUNNING → BUNDLING
  src/secretos.example.h  Plantilla de credenciales WiFi y URL del IoT Agent
  src/LoRaBoards.*, utilities.h   Soporte de placa de LilyGO (MIT)
nube/                     Servicios en Docker
  docker-compose.yml      Stack completo (FIWARE + servicios propios)
  .env.example            Plantilla de variables (SMTP, token y umbrales de alertas)
  processing/             ETL CrateDB → PostgreSQL (limpieza y clasificación)
  api/                    API FastAPI de lectura para el dashboard
  alertas/                Alertas reactivas (Flask): máquina de estados, correo y websocket. Con tests
  frontend/               Dashboard (HTML + Tailwind + Chart.js, servido por nginx)
docs/
  arquitectura.md         Cómo funciona el sistema completo, riesgos y protecciones
  alertas.md              Detalle del servicio de alertas
  conexion-sensor.md      Cableado, calibración y verificación del nodo real
  diagramas/              Diagramas editables (draw.io)
```

## Puesta en marcha

### Nube

Requiere Docker con Compose v2.

```bash
cd nube
cp .env.example .env          # llenar ALERTAS_TOKEN (openssl rand -hex 16) y SMTP_*
docker compose up -d --build
```

Después, crear las suscripciones de alertas (se puede repetir sin duplicar; volver a correr si cambian los umbrales):

```bash
docker exec fiware-alertas python suscripciones.py
```

Para probar sin la placa: `APIKEY=<apikey> alertas/demo.sh <servidor> 36`.

| Servicio | Puerto | Para qué |
|---|---|---|
| Dashboard | 80 | Interfaz web |
| API | 8000 | `/api/plantas/ultimo`, `/historico`, `/rango` |
| Alertas | 5050 | `/estado` y websocket `/ws` (lo usa el dashboard) |
| IoT Agent | 5555 | Recibe las tramas del nodo |
| Orion, IoT Agent (admin), CrateDB, QuantumLeap | 1026, 4041, 4200, 8668 | Administración: no exponer a internet |

### Firmware

Requiere [PlatformIO](https://platformio.org/). Abrir la carpeta `firmware/` en VS Code con la extensión de PlatformIO, o desde la terminal:

```bash
cd firmware
cp src/secretos.example.h src/secretos.h # WiFi y URL del IoT Agent con la apikey
pio run -t upload
pio device monitor
```

## Desarrollado por

- Daniel Ortiz Aristizábal
- Emanuel Londoño Osorio
- Valeria Zuluaga Alzate

---

Internet de las Cosas
