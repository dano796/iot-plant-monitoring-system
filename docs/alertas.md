# Sistema de alertas: cómo funciona de punta a punta

> Basado en el código de `nube/alertas/`, `nube/frontend/index.html` y `nube/docker-compose.yml` (2026-10-28).
> El resumen corto está en [arquitectura.md §12](arquitectura.md). Aquí está el detalle.

## 1. Idea general

El sistema de alertas **no consulta la base de datos ni hace polling de temperatura**. Funciona por eventos, con FIWARE:

1. La **regla** ("¿la temperatura está fuera de rango?") vive en **Orion**, como dos **suscripciones** NGSI-v2 con filtro `q`.
2. Cuando llega una trama que cumple el filtro, **Orion hace un POST** (una *notificación*) al servicio `alertas`.
3. El servicio `alertas` (Flask, puerto 5050) lleva una **máquina de estados** (`OK` / `ALARMA` / `SIN_DATOS`) para no alertar por una sola lectura rara.
4. Cada **cambio de estado** genera **un correo** y **un mensaje por websocket** al front, que lo pinta como banner.

```
T-Beam ──UL──► IoT Agent :5555 ──NGSI-v2 upsert──► Orion :1026
                                                     │
                     ┌───────────────────────────────┤ (2 suscripciones con q)
                     ▼                               │
           POST /data?token=...                      │ GET /v2/entities/Plant:plant001
           (notificación)                            │ (cada 30 s, para SIN_DATOS)
                     │                               │
                     ▼                               │
            ┌──────────────────── alertas (Flask :5050) ◄──┘
            │   máquina de estados (reglas.py)
            │        │
            │        ├──► PostgreSQL: alertas_estado / alertas_eventos
            │        │
            │        ├──► hilo "cartero"  ──► SMTP (Gmail) ──► correo
            │        └──► hilo "difusor"  ──► websocket /ws ──► navegador (banner)
            │
            └── GET /estado (estado actual, JSON)
```

Archivos:

| Archivo | Qué hace |
|---|---|
| `suscripciones.py` | Crea/actualiza en Orion las 2 suscripciones (la regla). Se corre a mano. |
| `main.py` | Servidor Flask: `/data`, `/estado`, `/ws`, y los 3 hilos de fondo. |
| `ngsi.py` | Traduce el JSON de Orion a `Lectura(entity_id, valor, ts)`. |
| `reglas.py` | Máquina de estados pura (sin red ni BD): umbrales, histéresis, confirmaciones. |
| `almacen.py` | Persistencia en PostgreSQL. |
| `correo.py` | Arma y envía el correo por SMTP. |
| `demo.sh` | Manda tramas falsas al IoT Agent para probar. |

---

## 2. Cómo llega el dato a Orion (contexto)

La placa manda `temperatura|36` en UltraLight al IoT Agent (`POST :5555/iot/d?k=<apikey>&i=plant001`). El agente hace un **upsert** en Orion de la entidad:

```json
{
  "id": "Plant:plant001",
  "type": "Plant",
  "temperatura": { "type": "Text", "value": 36, "metadata": { "TimeInstant": { "type": "DateTime", "value": "2026-10-28T17:00:00.000Z" } } },
  "TimeInstant": { "type": "DateTime", "value": "2026-10-28T17:00:00.000Z" }
}
```

Dos detalles que el sistema de alertas tiene en cuenta:

- El valor viaja como **número JSON**, pero con tipo `"Text"` (el dispositivo no declara atributos). Orion igual filtra numéricamente en `q`, y `ngsi.py` acepta número o string.
- Cada trama cambia `TimeInstant` (`IOTA_TIMESTAMP=true`). Ese timestamp es la "hora de la lectura" que usa la máquina de estados.

Todas las llamadas a Orion usan las cabeceras del tenant del IoT Agent:

```
fiware-service: openiot
fiware-servicepath: /
```

---

## 3. La regla vive en Orion: las suscripciones

`suscripciones.py` crea **dos** suscripciones sobre `Plant:plant001`. Con los umbrales por defecto (`TEMP_MIN=10`, `TEMP_MAX=30`, `HISTERESIS=2`, `VALIDO_MIN=0`, `VALIDO_MAX=60`):

| Descripción | `q` | Significado |
|---|---|---|
| `alertas: temperatura fuera de rango` | `temperatura!=10..30;temperatura==0..60` | Fuera de 10–30 °C **y** dentro del rango físico válido 0–60 |
| `alertas: temperatura dentro de rango` | `temperatura==12..28` | Dentro del rango "sano" (10+2 … 30−2) |

En la sintaxis de Orion `==a..b` es "entre a y b", `!=a..b` es "fuera de a..b", y `;` es AND.

Lo que **no** dispara ninguna suscripción:

- **Banda de histéresis** (10–12 y 28–30 °C): ni fuera ni dentro. A propósito: así una temperatura que oscila alrededor de 30 no genera correos en cada trama.
- **Lecturas inválidas** (< 0 o > 60 °C): un HDC1080 desconectado da 125 °C; Orion directamente no notifica.

### 3.1 El cuerpo de la suscripción

```json
{
  "description": "alertas: temperatura fuera de rango",
  "subject": {
    "entities": [{ "id": "Plant:plant001", "type": "Plant" }],
    "condition": {
      "attrs": ["temperatura"],
      "expression": { "q": "temperatura!=10..30;temperatura==0..60" },
      "alterationTypes": ["entityUpdate", "entityCreate"]
    }
  },
  "notification": {
    "httpCustom": { "url": "http://alertas:5050/data", "qs": { "token": "<ALERTAS_TOKEN>" } },
    "attrs": ["temperatura", "TimeInstant"],
    "attrsFormat": "normalized"
  }
}
```

Por qué cada campo:

- **`alterationTypes: [entityUpdate, entityCreate]`**: por defecto Orion solo notifica cuando un atributo **cambia** de valor. Con `entityUpdate` notifica **cada trama**, aunque repita `36`. Sin esto, tres lecturas seguidas de 36 serían una sola notificación y la ALARMA nunca se confirmaría.
- **`httpCustom.qs`**: Orion prohíbe `?` dentro de `http.url`, así que el token se pasa como query string con `httpCustom`. Orion termina llamando `POST http://alertas:5050/data?token=<ALERTAS_TOKEN>`.
- **`url: http://alertas:5050`**: Orion y `alertas` están en la misma red Docker (`fiware-net`); se resuelve por nombre del servicio, sin salir a internet.
- **`attrs`**: solo manda lo que se necesita (`temperatura` y `TimeInstant`).

### 3.2 Cómo se crean

```bash
docker exec fiware-alertas python suscripciones.py
```

- Hace `GET /v2/subscriptions`, busca por **descripción**.
- Si ya existe → `PATCH /v2/subscriptions/<id>` ("actualizada"); si no → `POST /v2/subscriptions` ("creada").
- Es **idempotente**: correrlo varias veces no duplica, y no toca otras suscripciones (por ejemplo la de QuantumLeap).
- Si cambian los umbrales en `.env`, hay que volver a correrlo: el `q` se calcula con ellos.
- Se niega a correr sin `ALERTAS_TOKEN`.

### 3.3 La notificación que recibe `/data`

```json
{
  "subscriptionId": "6720...",
  "data": [
    {
      "id": "Plant:plant001",
      "type": "Plant",
      "temperatura": { "type": "Text", "value": 36, "metadata": { "TimeInstant": { "...": "..." } } },
      "TimeInstant": { "type": "DateTime", "value": "2026-10-28T17:00:00.000Z", "metadata": {} }
    }
  ]
}
```

`ngsi.leer_notificacion()` convierte cada elemento de `data` en `Lectura("Plant:plant001", 36.0, 1793206800000)`:

- `valor`: número o string numérico → `float`; cualquier otra cosa → `None` (se tratará como inválida).
- `ts`: `TimeInstant` de la entidad (o el del atributo) en ms. **Si viene en el futuro se recorta a "ahora"**: el IoT Agent acepta `?t=` y una hora adelantada bloquearía todas las lecturas siguientes (ver idempotencia, §4.3).

---

## 4. El servicio `alertas`: la máquina de estados

### 4.1 Estados

| Estado | Significado |
|---|---|
| `OK` | Temperatura normal. Estado inicial. |
| `ALARMA` | Confirmado fuera de rango. |
| `SIN_DATOS` | Hace más de `SIN_DATOS_S` (120 s) que no llega ninguna trama. |

Inspirado en CloudWatch (`OK` / `ALARM` / `INSUFFICIENT_DATA`).

### 4.2 Clasificación de una lectura (`reglas.clasificar`)

| Clase | Condición (defaults) |
|---|---|
| `INVALIDA` | `None`, o fuera de 0–60 |
| `FUERA` | < 10 o > 30 |
| `DENTRO` | 12–28 |
| `BANDA` | 10–12 o 28–30 |

Notar que esto **repite** lo que filtran las suscripciones. Es a propósito: el servicio no confía en que Orion solo mande lo que debe (por ejemplo si alguien cambia una suscripción a mano).

### 4.3 Transiciones (`reglas.aplicar_lectura`)

```
                 3 lecturas FUERA seguidas
          ┌──────────────────────────────────────┐
          │                                      ▼
        ┌────┐                                ┌────────┐
        │ OK │                                │ ALARMA │
        └────┘                                └────────┘
          ▲                                      │
          └──────────────────────────────────────┘
                 3 lecturas DENTRO seguidas

   OK / ALARMA ──(> 120 s sin tramas)──► SIN_DATOS
   SIN_DATOS ──(llega una lectura)──► FUERA → ALARMA, DENTRO → OK, BANDA → estado previo
```

Paso a paso, para cada lectura:

1. **Idempotencia**: si `ts <= ultimo_ts` (ya se procesó esa trama o es una atrasada), se ignora. Por eso un reintento de Orion o una notificación duplicada no cuenta dos veces.
2. **`INVALIDA`** → se ignora por completo (ni siquiera actualiza `ultimo_ts`).
3. Se guarda `ultimo_ts = ts`.
4. **Si el estado es `SIN_DATOS`**: vuelve **directo**, sin confirmaciones, al estado que dice la lectura (`FUERA` → `ALARMA`, `DENTRO` → `OK`, `BANDA` → el estado que tenía antes de quedarse sin datos).
5. Si no: se mira si la lectura es **contraria** al estado actual (`FUERA` cuando está en `OK`, `DENTRO` cuando está en `ALARMA`):
   - contraria → `racha += 1`; si llega a `CONFIRMACIONES` (3) → **transición**.
   - `BANDA` → no suma ni reinicia la racha (histéresis).
   - cualquier otra (la que confirma el estado actual) → `racha = 0`.

Ejemplo, estando en `OK`:

| Trama | Clase | ¿Orion notifica? | racha | Estado |
|---|---|---|---|---|
| 36 | FUERA | sí (fuera) | 1 | OK |
| 29 | BANDA | **no** | 1 | OK |
| 37 | FUERA | sí (fuera) | 2 | OK |
| 20 | DENTRO | sí (dentro) | **0** | OK |
| 36, 36, 36 | FUERA | sí | 1, 2, 3 | **→ ALARMA** (correo + banner) |
| 36 ×5 | FUERA | sí | 0 | ALARMA (sin correos nuevos) |

Por eso hace falta la suscripción "dentro" incluso estando en `OK`: es la que reinicia la racha cuando una lectura alta fue un pico aislado.

### 4.4 `SIN_DATOS`: la revisión periódica

Si la placa se apaga, Orion no notifica nada, así que no hay evento que reaccione. Para eso hay un hilo `revisar` que **cada 30 s** (`REVISION_S`):

1. Hace `GET http://orion:1026/v2/entities/Plant:plant001` con las cabeceras de `openiot`.
2. Lee el `TimeInstant` actual de la entidad. Este campo se actualiza con **cada** trama, aunque esté en la banda y no haya notificado ninguna suscripción.
3. `reglas.revisar_periodica`:
   - si `ahora − TimeInstant > SIN_DATOS_S` (120 s) y no estaba ya en `SIN_DATOS` → **transición a `SIN_DATOS`**;
   - si estaba en `SIN_DATOS` y el `TimeInstant` volvió a ser reciente → aplica esa lectura como en §4.3 paso 4. Esto cubre el caso de que la placa vuelva con un valor en la banda (que ninguna suscripción notifica).
4. Si Orion responde 404 (la entidad no existe) o no responde, se cuenta desde la última trama que se vio; si nunca se vio ninguna, desde el arranque del servicio.

### 4.5 Qué se guarda en cada transición

El `Estado` guarda `valor` y `umbral` **de la transición**, no de la última lectura. Si no, el banner podría decir "ALARMA 25.0 °C" en mitad de una racha de vuelta a OK.

`umbral` es un texto para mostrar:

- `ALARMA` → `"> 30"` o `"< 10"` (según de qué lado del rango esté el valor);
- `OK` → `"12–28"`;
- `SIN_DATOS` → `None`.

---

## 5. Persistencia (PostgreSQL)

Usa el mismo Postgres del ETL (`plantas_db`). Las tablas se crean al arrancar (`iniciar_bd` reintenta cada 5 s hasta que Postgres responda).

**`alertas_estado`**: una fila por entidad, el estado actual.

| Columna | Contenido |
|---|---|
| `entity_id` | `Plant:plant001` (PK) |
| `estado` | `OK` / `ALARMA` / `SIN_DATOS` |
| `desde` | ms epoch del último cambio de estado |
| `previo` | estado antes de entrar a `SIN_DATOS` |
| `racha` | lecturas contrarias seguidas |
| `ultimo_ts` | `TimeInstant` de la última lectura válida procesada |
| `valor`, `umbral` | los de la transición |

**`alertas_eventos`**: una fila por transición, funciona como **cola de correos**.

| Columna | Contenido |
|---|---|
| `id` | serial (orden de envío) |
| `entity_id`, `de`, `a`, `valor`, `umbral`, `ts`, `ultima_trama` | la transición |
| `enviado`, `enviado_en` | si ya salió el correo y cuándo |

El estado y el evento se escriben **en la misma transacción** (`almacen.guardar`): nunca queda un correo pendiente sin su estado, ni un estado sin su correo. Y como todo está en Postgres, **reiniciar el contenedor no repite correos ni pierde la racha**.

---

## 6. Concurrencia: por qué hay hilos

`/data` lo llama Orion. Si `/data` esperara al SMTP (que puede tardar segundos o colgarse), Orion acumularía notificaciones y podría marcar la suscripción como fallida. Por eso `/data` solo **calcula y guarda**, y avisa a otros hilos:

| Hilo | Qué hace |
|---|---|
| Flask (`threaded=True`) | Atiende `/data`, `/estado`, `/ws`. |
| `revisar` | Revisión periódica de `SIN_DATOS` (§4.4). |
| `cartero` | **Único** que envía correos. Despierta con cada transición (`hay_eventos.set()`) o cada 30 s para reintentar. |
| `difusor` | **Único** que escribe en los websockets. Lee de la cola `difusion`. |

Detalles:

- `lock_estado`: `cargar → aplicar → guardar` es atómico entre `/data` y `revisar`. Sin él, una notificación y la revisión periódica podrían pisarse.
- Un solo `cartero` garantiza que **un pendiente nunca sale dos veces**.
- Un solo `difusor` porque `simple-websocket` no admite dos `send` simultáneos sobre el mismo socket.
- `difundir` copia la lista de clientes y envía **fuera del lock**: un navegador lento no bloquea conexiones nuevas. Si un `send` falla, ese cliente se descarta.

`actualizar()` es el punto común: si hubo transición, imprime el log, pone el mensaje en `difusion` y despierta al `cartero`.

---

## 7. Correos (`correo.py`)

El `cartero` recorre `alertas_eventos WHERE NOT enviado ORDER BY id`:

- **Sin `ALERTA_DESTINO`** → el evento se marca como enviado y se descarta (queda en el log). Así, al configurar el correo más tarde no llega una ráfaga vieja.
- **Pendiente con más de `PENDIENTE_MAX_S` (1 h)** → descartado por la misma razón.
- **Falla el SMTP** → se detiene (`break`) y reintenta en la próxima vuelta, **conservando el orden** (no llega un "OK" antes que su "ALARMA").

Formato (estilo CloudWatch), asunto según el estado:

```
ALARMA: temperatura-plant001 en ALARMA (36.0 °C > 30 °C)
OK: temperatura-plant001 volvió a OK (25.0 °C)
SIN DATOS: temperatura-plant001 sin datos desde 14:32
```

Cuerpo:

```
temperatura-plant001: OK → ALARMA

Valor: 36.0 °C (> 30 °C)
Hora: 2026-10-28 12:00:00
Regla: entra en ALARMA fuera de 10–30 °C; vuelve a OK dentro de 12–28 °C (3 lecturas seguidas).

Dashboard: http://<FRONT_URL>
```

Las horas se muestran en `America/Bogota` (`ZONA`). Puerto 465 → `SMTP_SSL` (Gmail con contraseña de aplicación); otro puerto → SMTP plano (útil con mailpit en local).

---

## 8. El front: cómo se muestra

El dashboard (`nube/frontend/index.html`, servido por nginx en :80) habla **directo** con el servicio de alertas en el puerto 5050 del mismo host. No pasa por la API FastAPI ni por Orion:

```js
const ALERTAS_URL = "http://" + window.location.hostname + ":5050";
const WS_URL      = "ws://"   + window.location.hostname + ":5050/ws";
```

Por eso el puerto **5050 tiene que estar abierto** en el security group de la EC2. Los CORS están abiertos (`Access-Control-Allow-Origin: *`).

### 8.1 Conexión

```js
function conectarAlertas() {
  fetch(`${ALERTAS_URL}/estado`).then(...).then(m => m && pintarAlerta(m));  // estado actual al cargar
  const ws = new WebSocket(WS_URL);
  ws.onmessage = ev => pintarAlerta(JSON.parse(ev.data));                  // cada transición, en vivo
  ws.onclose   = () => setTimeout(conectarAlertas, 3000);                  // reconexión silenciosa
}
```

1. Al cargar la página, `GET /estado` trae el estado actual, para que una alarma que empezó antes de abrir la página se vea de inmediato.
2. Abre el websocket `/ws`. El servidor lo agrega a `clientes` y le manda también el estado actual apenas conecta.
3. Desde ahí, cada transición llega como un mensaje.
4. Si el servicio se cae o se reinicia, el navegador reintenta cada 3 s.

El websocket es **solo de servidor a navegador**: el front no envía nada; `ws.receive()` en el servidor solo sirve para detectar cuándo se cierra.

### 8.2 El mensaje

```json
{
  "entity_id": "Plant:plant001",
  "estado": "ALARMA",
  "desde": 1793206800000,
  "valor": 36.0,
  "umbral": "> 30",
  "ultimo_ts": 1793206800000,
  "de": "OK"
}
```

`de` es el estado anterior. Viene solo en los mensajes de transición; en `/estado` y en el primer mensaje del websocket es `null`.

### 8.3 Qué pinta `pintarAlerta(msg)`

| Caso | Banner (`#banner-alerta`) | Tarjeta de temperatura (`#card-temp`) |
|---|---|---|
| `estado = ALARMA` | **Rojo**, fijo: `⚠ ALARMA · plant001 · Temperatura 36.0 °C > 30 °C · desde 12:00:00` | Borde rojo grueso y valor en rojo |
| `estado = SIN_DATOS` | **Gris**, fijo: `SIN DATOS · plant001 · última trama 11:58:00` | Normal |
| `estado = OK` y `de` es `ALARMA` o `SIN_DATOS` | **Verde** por 5 s: `✓ OK · plant001 · Temperatura 25.0 °C` | Normal |
| `estado = OK` sin transición (carga inicial) | Oculto | Normal |

El banner de alertas es independiente del polling de cada 5 s que hace el dashboard a la API (`fetchData`): las gráficas y valores vienen de FastAPI/PostgreSQL; el banner viene del websocket.

---

## 9. Seguridad

- **`ALERTAS_TOKEN` es obligatorio.** `/data` exige `?token=<ALERTAS_TOKEN>` y lo compara con `hmac.compare_digest` (tiempo constante). Si la variable está vacía, `/data` devuelve **403 a todo** (falla cerrado) y `suscripciones.py` no corre. Sin esto, cualquiera con acceso al puerto 5050 podría inventar alarmas.
- **Solo se procesa `ENTIDAD`** (`Plant:plant001`). El IoT Agent tiene autoregistro activo, así que un dispositivo inventado crearía otra entidad; esas notificaciones se ignoran.
- **Hora futura recortada** (§3.3).
- **Lecturas inválidas ignoradas**, en Orion (`q`) y en el servicio (`clasificar`).
- **Umbrales validados al arrancar** (`Config.__post_init__`): si `HISTERESIS` deja vacío el rango de vuelta a OK, si `TEMP_MIN–TEMP_MAX` se sale del rango válido o si `CONFIRMACIONES < 1`, el servicio **no arranca** y dice por qué. Sin esa validación, una configuración imposible fallaría en silencio (una ALARMA que nunca vuelve a OK).
- `/estado` y `/ws` son públicos y de solo lectura: exponen el estado de la alarma, nada más.

---

## 10. Configuración

En `nube/.env` (ignorado por git; plantilla en `nube/.env.example`), pasado por `docker-compose.yml`:

| Variable | Default | Uso |
|---|---|---|
| `ALERTAS_TOKEN` | — (**obligatoria**) | Secreto de `/data`. `openssl rand -hex 16` |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `465` | Servidor de correo |
| `SMTP_USER` / `SMTP_PASS` | — | Cuenta y contraseña de aplicación |
| `ALERTA_DESTINO` | — | Destinatarios, separados por coma |
| `FRONT_URL` | — | Link al dashboard en el correo |
| `TEMP_MIN` / `TEMP_MAX` | 10 / 30 | Rango normal |
| `HISTERESIS` | 2 | Margen para volver a OK |
| `CONFIRMACIONES` | 3 | Lecturas seguidas para cambiar de estado |
| `SIN_DATOS_S` | 120 | Segundos sin tramas para `SIN_DATOS` |
| `VALIDO_MIN` / `VALIDO_MAX` | 0 / 60 | Rango físico válido |

Con defaults en el código (no están en el compose): `ORION_URL` (`http://orion:1026`), `ENTIDAD` (`Plant:plant001`), `PG_DSN`, `ZONA` (`America/Bogota`), `REVISION_S` (30), `PENDIENTE_MAX_S` (3600), y para `suscripciones.py` `NOTIFY_URL` (`http://alertas:5050/data`).

> Si se cambian los umbrales: `docker compose up -d alertas` **y** `docker exec fiware-alertas python suscripciones.py` (el `q` de Orion se recalcula). Si se cambia código: `docker compose up -d --build alertas`.

---

## 11. Cómo verlo funcionar

```bash
# Suscripciones en Orion
curl -s localhost:1026/v2/subscriptions -H fiware-service:openiot -H fiware-servicepath:/ | jq '.[] | {description, status, q: .subject.condition.expression.q, timesSent, lastNotification}'

# Estado actual de la alarma
curl -s localhost:5050/estado | jq

# Logs del servicio (transiciones, correos, errores)
docker logs -f fiware-alertas

# Eventos guardados
docker exec fiware-postgres psql -U admin -d plantas_db -c "SELECT * FROM alertas_eventos ORDER BY id DESC LIMIT 10;"

# Demo: 3 tramas de 36 °C → ALARMA; luego 3 de 25 °C → OK
APIKEY=<apikey> nube/alertas/demo.sh <servidor> 36
APIKEY=<apikey> nube/alertas/demo.sh <servidor> 25
```

| Demo | Resultado esperado |
|---|---|
| `demo.sh <servidor> 36` (3 tramas) | 1 correo ALARMA, banner rojo |
| `demo.sh <servidor> 36 5` | ningún correo nuevo |
| `demo.sh <servidor> 29` | nada cambia (banda) |
| `demo.sh <servidor> 25` | 1 correo OK, banner verde 5 s |
| Sin tramas > 2 min (placa apagada) | correo SIN DATOS, banner gris |
| `demo.sh <servidor> 125` | nada (lectura inválida) |

Tests: `cd nube/alertas && pip install -r requirements.txt -r requirements-dev.txt && pytest` (`reglas`, `ngsi`, `correo`, `suscripciones` y `main`).
