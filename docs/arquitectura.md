# Cómo funciona el sistema

> Visión de conjunto del sistema. El detalle del servicio de alertas está en [alertas.md](alertas.md) y el montaje del hardware en [conexion-sensor.md](conexion-sensor.md). Diagrama editable: [diagramas/sistema.drawio](diagramas/sistema.drawio).

```
T-Beam (sensores) ──HTTP POST UltraLight──► IoT Agent :5555 ──NGSI-v2──► Orion :1026 ──► MongoDB (estado actual)
       │                                                                     │
       └─LoRa (se transmite, pero nadie lo recibe)                          suscripción ──(2 suscripciones con regla)──► alertas (Flask) :5050 ──► correo + websocket al front
                                                                             ▼
                                                                QuantumLeap :8668 ──► CrateDB (histórico crudo)
                                                                                            │ cada 60 s
                                                                                            ▼
                                                    data-processing (ETL) ──► PostgreSQL (datos limpios)
                                                                                            │
                                                                     API FastAPI :8000 ◄────┘
                                                                            │
                                                                     Front nginx :80
```

## 1. El emisor: qué mide y cómo envía

El firmware ([firmware/src/main.cpp](../firmware/src/main.cpp)) funciona como una máquina de 3 estados que se repite cada **10 s** (`PERIODO_TRAMA`):

1. **CHIRP:** toma **3 muestras** de cada sensor, separadas 1 s (`T_RECUPERACION`). Texas Instruments recomienda leer el HDC1080 menos de una vez por segundo para que no se caliente solo. Los sensores son:
   - HDC1080: temperatura y humedad del aire, por I2C (pines 21/22).
   - AP3216: luz, por I2C.
   - Sensor de suelo: valor analógico en GPIO36 (ADC1, compatible con WiFi).
   - Mientras espera entre muestras, sigue leyendo el GPS.
2. **PRUNNING:** combina las 3 muestras de cada variable con una **mediana**. Así descarta un valor atípico en vez de promediarlo.
3. **BUNDLING:** arma la trama y la envía.

**Método de envío:** **HTTP POST** en texto plano, en formato **UltraLight 2.0**, al IoT Agent:

```
POST http://<servidor>:5555/iot/d?k=<apikey>&i=plant001
Content-Type: text/plain

temperatura|24.5|humedad|61.2|humedadSuelo|40|luz|120.6|lat|4.711|lon|-74.07
```

- `k` es la apikey del servicio `openiot` e `i` es el id del dispositivo. Los dos están en `firmware/src/secretos.h`, que no se sube a git (plantilla: `secretos.example.h`).
- La misma trama también sale por **LoRa** (915 MHz, SF10, BW 125 kHz, sync word 0xAB). Hoy **nadie la recibe**: quedó de la arquitectura anterior y sirve si más adelante montan un gateway LoRa.

## 2. IoT Agent (`fiware/iotagent-ul`): el receptor

> Verificado el 2026-10-16 contra el código que corre en el contenedor `fiware-iot-agent` (iotagent-ul **3.7.0** sobre iotagent-node-lib **4.7.0**, en `/opt/iotaul`) y contra sus logs en nivel DEBUG.

El IoT Agent es un **traductor**: recibe texto UltraLight del dispositivo por el puerto **5555** ("southbound") y lo convierte en una entidad NGSI-v2 que envía a Orion ("northbound"). No guarda historia ni procesa los datos. Tiene un segundo puerto, **4041**, que es su API de administración: ahí se crean los grupos de servicio y los dispositivos.

### 2.1 Qué recibe

La T-Beam hace esta petición cada 10 s:

```
POST /iot/d?k=<apikey>&i=plant001 HTTP/1.1
Host: <servidor>:5555
Content-Type: text/plain

temperatura|24.936523|humedad|92.852783|humedadSuelo|45.857418|luz|91.800003|lat|0.000000|lon|0.000000
```

| Parte | Significado | Si falta o está mal |
|---|---|---|
| `/iot/d` | Ruta del agente (valor por defecto; debe coincidir con el `resource` del grupo) | 404 |
| `k` | apikey: identifica el **grupo de servicio** | 404 `DEVICE_GROUP_NOT_FOUND` |
| `i` | id del dispositivo | Error `MANDATORY_PARAMS_NOT_FOUND` |
| `Content-Type: text/plain` | Obligatorio en el POST | Error `UNSUPPORTED_TYPE` |
| `t` (opcional, no lo usamos) | Hora de la medición; reemplaza la hora de llegada | — |
| Cuerpo | Medidas en UltraLight 2.0 | Error `PARSE_ERROR` |

También acepta **GET** con la trama en el parámetro `d` (`GET /iot/d?k=...&i=...&d=t|25`), pero el firmware usa POST.

**Formato UltraLight 2.0** ([lib/ulParser.js](https://github.com/telefonicaid/iotagent-ul/blob/master/lib/ulParser.js)):
- `|` separa claves y valores: `clave|valor|clave|valor`. Debe haber un número **par** de campos y **ninguno vacío**. Por ejemplo, `t|` se rechaza.
- `#` separa varias mediciones en un mismo envío (`t|20#t|21`).
- Si un grupo tiene un número impar de campos, el primero se toma como fecha: `2026-10-16T19:00:00Z|t|20`.
- Si un grupo contiene `@` (`plant001@riego|on`), se interpreta como **respuesta a un comando**, no como medición.

### 2.2 Qué hace por dentro, paso a paso

Rutas de Express en [lib/bindings/HTTPBindings.js](https://github.com/telefonicaid/iotagent-ul/blob/master/lib/bindings/HTTPBindings.js): `checkMandatoryParams → parseData → addTimestamp → handleIncomingMeasure → returnCommands`.

1. **Valida** que vengan `k`, `i` y el `Content-Type`.
2. **Parsea** la trama. Cada valor pasa por `JSON.parse`: `"24.936523"` se convierte en el número `24.936523`, `"0.000000"` en `0` y `"true"` en un booleano. Si `JSON.parse` falla (por ejemplo, con `"abc"`), el valor queda como texto.
   ```json
   {"temperatura":24.936523,"humedad":92.852783,"humedadSuelo":45.857418,"luz":91.800003,"lat":0,"lon":0}
   ```
3. **Busca el grupo de servicio** en Mongo (base `iota_ul`, colección `groups`) por `resource=/iot/d` + `apikey`. De ahí obtiene `service=openiot`, `subservice=/` y `entity_type=Plant`.
4. **Busca el dispositivo** (colección `devices`) por `id=plant001` + `apikey` + servicio. **Si no existe, lo crea** (autoprovisión), porque el grupo no tiene `autoprovision: false`. El nombre de la entidad por defecto es `<tipo>:<id>`, o sea `Plant:plant001`.
5. **Asigna el tipo** de cada atributo (`guessType` en `lib/commonBindings.js`). Si el atributo está declarado en el dispositivo (`active`), usa ese tipo. Si no, usa **`Text`**. Como `plant001` no tiene atributos declarados, **los 6 salen como `Text`, aunque el valor sea un número**.
6. **Agrega la marca de tiempo:** con `IOTA_TIMESTAMP=true`, añade un atributo `TimeInstant` (tipo `DateTime`, con la hora de llegada al agente) y lo copia como metadato en cada atributo.
7. **Envía la entidad a Orion** (siguiente apartado).
8. **Guarda en Mongo la última medición** del dispositivo (`oldCtxt`). Así, cada trama implica también una escritura en `iota_ul.devices`.
9. **Responde a la T-Beam `200` con cuerpo vacío**. Si hubiera comandos en cola y la petición trajera `getCmd=1`, los devolvería en esa misma respuesta. En total tarda unos **45 ms**.

### 2.3 Cómo lo manda a Orion

Una sola petición por trama: un **upsert** en Orion (crea la entidad si no existe; si existe, reemplaza los atributos enviados y deja intactos los demás):

```
POST http://orion:1026/v2/entities?options=upsert
fiware-service: openiot
fiware-servicepath: /
Content-Type: application/json
```
```json
{
  "id": "Plant:plant001",
  "type": "Plant",
  "temperatura":  {"type": "Text", "value": 24.936523, "metadata": {"TimeInstant": {"type": "DateTime", "value": "2026-10-16T19:22:16.831Z"}}},
  "humedad":      {"type": "Text", "value": 92.852783, "metadata": {"TimeInstant": {"type": "DateTime", "value": "2026-10-16T19:22:16.831Z"}}},
  "humedadSuelo": {"type": "Text", "value": 45.857418, "metadata": {"TimeInstant": {"type": "DateTime", "value": "2026-10-16T19:22:16.831Z"}}},
  "luz":          {"type": "Text", "value": 91.800003, "metadata": {"TimeInstant": {"type": "DateTime", "value": "2026-10-16T19:22:16.831Z"}}},
  "lat":          {"type": "Text", "value": 0,         "metadata": {"TimeInstant": {"type": "DateTime", "value": "2026-10-16T19:22:16.831Z"}}},
  "lon":          {"type": "Text", "value": 0,         "metadata": {"TimeInstant": {"type": "DateTime", "value": "2026-10-16T19:22:16.831Z"}}},
  "TimeInstant":  {"type": "DateTime", "value": "2026-10-16T19:22:16.831Z"}
}
```

- El **`fiware-service`** hace que Orion guarde la entidad en una base Mongo aparte (`orion-openiot`), no en la base por defecto. Cualquier consulta a Orion tiene que llevar ese encabezado, o no encontrará la entidad.
- Orion contesta sin cuerpo (`204` si actualiza; `201` si crea la entidad). El agente lo registra como `Value updated successfully`.
- Si Orion falla, el agente le devuelve el error a la T-Beam (registrado como `MEASURES-002`) y **la medición se pierde**: el agente no reintenta ni guarda en cola.

### 2.4 Consecuencia: el tipo `Text`

El **valor** viaja como número JSON, pero el **tipo** declarado es `Text`. QuantumLeap crea las columnas de CrateDB según el **tipo**, no según el valor: por eso en `mtopeniot.etplant` las columnas `temperatura`, `humedad`, `humedadsuelo`, `luz`, `lat` y `lon` son `text`, y el ETL tiene que convertirlas con `to_numeric`. Si se declaran como `Number` en el dispositivo (sección 11), las columnas **nuevas** se crean como `double`. Las ya existentes no cambian de tipo: habría que borrar la tabla o migrarla.

### 2.5 Cómo inspeccionarlo

```bash
# Configuración del grupo de servicio y del dispositivo
curl -s localhost:4041/iot/services -H fiware-service:openiot -H fiware-servicepath:/
curl -s localhost:4041/iot/devices  -H fiware-service:openiot -H fiware-servicepath:/

# Ver en vivo cada trama: payload parseado y petición enviada a Orion (IOTA_LOG_LEVEL=DEBUG)
docker logs -f fiware-iot-agent 2>&1 | grep -E "Parsing payload|v2/entities|successfully updated|MEASURES-"

# Mandar una trama a mano
curl -X POST "localhost:5555/iot/d?k=<apikey>&i=plant001" \
  -H "Content-Type: text/plain" -d "temperatura|25|humedad|60"

# Ver lo que quedó en Orion
curl -s localhost:1026/v2/entities/Plant:plant001 -H fiware-service:openiot -H fiware-servicepath:/
```

## 3. Orion Context Broker: el estado actual

- Orion guarda **solo el último valor** de cada entidad. Cada trama **sobrescribe** la entidad `Plant:plant001` en **MongoDB** (una actualización, no un insert). No guarda historia.
- Tiene una **suscripción**: cada vez que cambia una entidad de tipo `Plant`, le notifica el cambio a QuantumLeap (`http://quantumleap:8668/v2/notify`).
- Es el orquestador: no procesa los datos, los reparte. Cualquier sistema puede consultarle el estado en vivo con `GET /v2/entities`.

## 4. QuantumLeap: el histórico

- Recibe cada notificación de Orion y la **inserta como una fila nueva** en **CrateDB**, una base de datos para series de tiempo.
- La tabla se nombra a partir del servicio y el tipo: `mtopeniot.etplant` (`mt` + servicio, `et` + tipo). Tiene una columna por atributo, más `entity_id` y `timeinstant`.
- En resumen: **MongoDB tiene el ahora; CrateDB tiene todo lo que pasó**, en datos crudos y sin limpiar.

## 5. Cómo se actualiza cada base de datos

| Base | Quién escribe | Cómo | Qué contiene |
|---|---|---|---|
| MongoDB | Orion | Sobrescribe (una entidad por dispositivo) | Estado actual |
| CrateDB | QuantumLeap | Agrega una fila por cada cambio | Histórico crudo |
| PostgreSQL | ETL | Agrega filas de forma incremental (solo las nuevas) | Histórico limpio y clasificado |

## 6. ETL / análisis (`data-processing`)

El código está en [nube/processing/main.py](../nube/processing/main.py) y corre en un ciclo cada 60 s (`PERIODO_S`).

| Etapa | Qué hace |
|---|---|
| **Extract** | Busca en Postgres el último `timestamp` ya procesado y lee de CrateDB **solo las filas más nuevas**. Así no duplica datos. Si no encuentra las columnas nuevas, intenta con los nombres viejos (`temp`, `h_aire`, `h_suelo`). |
| **Transform: limpieza** | Convierte el texto a número (`to_numeric`; lo que no se puede convertir queda vacío). Descarta las filas fuera de rango: temperatura fuera de 0–60 °C, humedad del aire o del suelo fuera de 0–100 %, o con valores vacíos. |
| **Transform: analítica** | Clasifica cada medición según la humedad de suelo: menos de 30 → `NECESITA_RIEGO`, más de 80 → `EXCESO_AGUA`, entre 30 y 80 → `OPTIMO`. |
| **Load** | Inserta las filas limpias en **PostgreSQL**, en la tabla `mediciones_limpias`, que se crea sola si no existe. |

Lo que hace hoy es **limpieza y clasificación por umbrales**, no analítica estadística: no calcula promedios, tendencias ni alertas en el tiempo.

## 7. Entrega al front

- La **API FastAPI** (`:8000`) consulta Postgres en cada petición:
  - `GET /api/plantas/ultimo` → la última medición limpia.
  - `GET /api/plantas/historico` → las últimas 50.
  - `GET /api/plantas/rango?desde=&hasta=&puntos=` → promedios por ventana entre dos instantes (ms epoch), para la pestaña de históricos.
  - Tiene CORS abierto (`*`) para que el navegador pueda llamarla desde el front.
- El **front** es un `index.html` servido por nginx en el puerto 80 que consume esa API.
- **Retraso total:** desde la medición hasta el front puede pasar ~1 minuto o algo más. La medición toma unos 10 s, el ETL corre cada 60 s y a eso se suma lo que tarde el front en refrescar.

## 8. ¿Qué pasa si no llega una trama NMEA completa?

El GPS integrado (u-blox NEO-6M) envía frases NMEA por serial a 9600 baudios, en los pines 34/12. `smartDelay()` le pasa cada carácter a **TinyGPSPlus** con `gps.encode()`.

- **Frase incompleta o corrupta:** TinyGPSPlus verifica el **checksum** de cada frase. Si una llega cortada o con un checksum malo, la descarta completa: **no actualiza** latitud ni longitud con datos a medias. Una trama rota nunca produce coordenadas malas.
- **Nunca hubo posición** (por ejemplo, en interiores): `location.isValid()` es falso y en el serial aparece `GPS sin fix (sats=…, chars=…)`.
  - `chars=0`: no llega nada del GPS (cableado o alimentación).
  - `chars>0`: el GPS habla, pero le falta cielo abierto.
  - **La trama sale igual con `lat=0, lon=0`**, porque las variables empiezan en 0. El ETL no filtra coordenadas, así que ese 0,0 llega al histórico.
- **Tuvo posición y la perdió:** `isValid()` **sigue siendo verdadero**, porque TinyGPSPlus mantiene la última posición conocida. Se sigue enviando esa posición **sin avisar que está vieja**: el código no revisa `gps.location.age()`.

## 9. Protecciones existentes

**En el emisor:**
- La mediana de 3 descarta valores atípicos.
- La humedad de suelo se limita a 0–100 % (`constrain`).
- Al arrancar revisa si el HDC1080 y el AP3216 responden, pero **solo lo escribe en el serial**: igual sigue enviando.
- Solo intenta el POST si hay WiFi. Si no hay, la trama sale solo por LoRa, y como nadie la recibe, se pierde.
- El WiFi se reconecta solo si se cae.
- El checksum NMEA lo maneja TinyGPSPlus.

**En la nube:**
- El IoT Agent exige la apikey y el `device_id`.
- El ETL descarta valores fuera de rango o vacíos. Así se filtra la temperatura de 125 °C que da un HDC1080 desconectado.
- El ETL no duplica filas, porque procesa solo lo nuevo según el último timestamp guardado.
- Si CrateDB o Postgres fallan, el ETL captura el error, lo registra y vuelve a intentar en el siguiente ciclo.
- `restart: unless-stopped` en el procesamiento y en Postgres.
- MongoDB (27017) y PostgreSQL (5433) **no** están abiertos a internet.

## 10. Lo que falta proteger

| Riesgo | Detalle |
|---|---|
| **Pérdida de datos** | Si el POST falla, la trama se pierde: no hay reintento ni buffer en el emisor. |
| **Sin cifrado** | Todo va por HTTP, y la apikey viaja visible en la URL. La trama LoRa tampoco va cifrada, y además tiene el CRC desactivado (`disableCrc`). |
| **Puertos de administración abiertos** | 1026 (Orion), 4041 (IoT Agent), 4200 (CrateDB, cualquiera puede borrar tablas) y 8668 (QuantumLeap) están expuestos sin autenticación. |
| **Autoregistro** | Con la apikey, cualquiera puede mandar datos con otro `i=` y el IoT Agent crea el dispositivo automáticamente (comportamiento por defecto). |
| **Credenciales en el código** | Postgres usa `admin/adminpassword` escrito directamente en el código de la API y del ETL. |
| **API** | No tiene autenticación, y ante un error devuelve el detalle interno (`str(e)`). |
| **Luz sin filtro** | Si el AP3216 falla, envía 23592 lx y el ETL lo deja pasar, porque no valida la luz. |
| **GPS** | Se envía 0,0 sin posición y la última posición conocida sin indicar su antigüedad (sección 8). |
| **Un solo marcador de avance** | El ETL usa un único "último timestamp" para todos los dispositivos. Con varios sensores, una fila atrasada de uno de ellos se saltaría. |

## 11. ¿PATCH o batch? Y el riesgo de "sensores falsos"

**Qué método se usa en cada tramo:**

| Tramo | Método |
|---|---|
| Emisor → IoT Agent | `POST /iot/d`, **una trama por medición**. No es batch ni PATCH. |
| IoT Agent → Orion | `POST /v2/entities?options=upsert`, una entidad por trama. No es batch. El *upsert* **crea la entidad si no existe** (sección 2.3). |
| Orion → QuantumLeap | Notificación `POST /v2/notify`, una por cambio. |
| ETL → PostgreSQL | `INSERT` fila por fila. |

**El riesgo real no es el batch en sí, sino quién puede crear entidades.** Hoy hay tres formas:

1. **Autoregistro en el IoT Agent:** con la apikey, un `POST /iot/d?k=<apikey>&i=cualquier_cosa` crea un dispositivo y una entidad nuevos. Así cualquiera puede inundar el sistema de sensores falsos.
2. **Orion expuesto (1026):** cualquiera puede saltarse el IoT Agent y crear miles de entidades en una sola petición con `POST /v2/op/update`, o con `POST /v2/entities`. Esta es la puerta más grave.
3. **Atributos libres:** el dispositivo acepta cualquier clave en la trama (`hack|123` se convierte en un atributo nuevo).

**Cómo cerrarlo:**

```bash
# 1. Desactivar el autoregistro en el grupo de servicio: solo plant001 puede enviar
curl -X PUT "localhost:4041/iot/services?resource=/iot/d&apikey=<apikey>" \
  -H "fiware-service: openiot" -H "fiware-servicepath: /" -H "Content-Type: application/json" \
  -d '{"autoprovision": false}'

# 2. Declarar los atributos como Number y aceptar solo esos (explicitAttrs).
#    Además arregla que CrateDB guarde los valores como texto.
curl -X PUT "localhost:4041/iot/devices/plant001" \
  -H "fiware-service: openiot" -H "fiware-servicepath: /" -H "Content-Type: application/json" \
  -d '{"explicitAttrs": true, "attributes": [
        {"object_id":"temperatura","name":"temperatura","type":"Number"},
        {"object_id":"humedad","name":"humedad","type":"Number"},
        {"object_id":"humedadSuelo","name":"humedadSuelo","type":"Number"},
        {"object_id":"luz","name":"luz","type":"Number"},
        {"object_id":"lat","name":"lat","type":"Number"},
        {"object_id":"lon","name":"lon","type":"Number"}]}'

# 3. Cerrar 1026, 4041, 4200 y 8668 en el security group, o publicarlos solo en 127.0.0.1 en el compose.
```

**Cómo validar que quedó protegido** (con la EC2 encendida):

| Prueba | Resultado esperado |
|---|---|
| POST con `i=plant001` y la apikey correcta | 200, y el dato llega a Orion |
| POST con `i=falso999` | Error (dispositivo no encontrado); no se crea la entidad `Plant:falso999` |
| POST con `...|hack|123` | 200, pero `hack` no aparece en la entidad |
| POST con apikey incorrecta | Error |
| `curl <servidor>:1026/version` desde fuera de la EC2 | Sin conexión (timeout) |
| `docker exec fiware-mongo mongo iota_ul --eval "db.devices.count()"` | Sigue en 1 |

Un detalle de los atributos: si `lat` y `lon` se declaran `Number`, el valor 0,0 sin posición seguiría pasando como número válido. Eso se resuelve en el firmware (sección 8).

**Mejoras baratas sugeridas:**
- No enviar la posición si `gps.location.age()` supera unos segundos, y no enviar 0,0 cuando no hay posición.
- Agregar un filtro de rango para la luz en el ETL.
- Cerrar los puertos 1026, 4041, 4200 y 8668 en el security group de AWS.

## 12. Alertas reactivas (`nube/alertas/`)

Detalle completo en [alertas.md](alertas.md). Diagrama editable: [diagramas/alertas.drawio](diagramas/alertas.drawio).

**La regla vive en Orion:** dos suscripciones sobre la entidad `Plant:plant001` notifican a `http://alertas:5050/data`. Usan `alterationTypes: [entityUpdate, entityCreate]`, así que cada trama notifica aunque repita el valor (por defecto Orion solo notifica cambios).

| Suscripción | `q` | Para qué |
|---|---|---|
| `alertas: temperatura fuera de rango` | `temperatura!=10..30;temperatura==0..60` | Entrar en ALARMA (fuera de rango, pero dentro del rango válido) |
| `alertas: temperatura dentro de rango` | `temperatura==12..28` | Volver a OK (histéresis de 2 °C) |

El servicio `alertas` (Flask) lleva una máquina de estados:

- **ALARMA:** se entra con 3 lecturas seguidas fuera de rango y se sale con 3 seguidas dentro de 12–28 °C. Las lecturas de 28–30 y 10–12 °C son neutras.
- **SIN_DATOS:** si el `TimeInstant` de la entidad en Orion tiene más de 120 s. Se revisa cada 30 s, y al volver los datos se cambia de estado de inmediato. Si Orion no tiene la entidad (404) o no responde, se cuenta desde la última trama vista.

Cada transición produce **un correo** (estilo CloudWatch) y un mensaje por websocket que el front muestra como banner. Los dos salen en hilos aparte: `/data` responde a Orion sin esperar al SMTP. Las lecturas fuera de 0–60 °C se **ignoran**, igual que en el ETL: un HDC1080 desconectado (125 °C) no genera alarma.

**Contra duplicados:**

- Una notificación con un `TimeInstant` ya procesado se ignora.
- El estado y los eventos se guardan en Postgres (`alertas_estado` y `alertas_eventos`), así que un reinicio no repite correos.
- Un correo que falla queda pendiente y se reintenta cada 30 s.
- Para no mandar después una ráfaga atrasada:
  - si no hay `ALERTA_DESTINO`, el evento se descarta (queda en el log);
  - un pendiente con más de `PENDIENTE_MAX_S` (3600 s) también se descarta.

**Seguridad:**

- `ALERTAS_TOKEN` es **obligatorio**: sin él, `/data` rechaza todo y `suscripciones.py` no corre. Con él, `/data` exige `?token=...`. Orion lo envía por `httpCustom.qs`, porque prohíbe `?` en `http.url`.
- Solo se procesa `Plant:plant001` (variable `ENTIDAD`).
- Una trama con hora en el futuro (el IoT Agent acepta `?t=`) se toma con la hora actual. Así no puede bloquear las lecturas siguientes.

**Variables** (en `nube/.env`, ignorado por git; plantilla en `nube/.env.example`):

- correo: `SMTP_USER`, `SMTP_PASS` (contraseña de aplicación de Gmail), `ALERTA_DESTINO` (separados por coma);
- `FRONT_URL` y `ALERTAS_TOKEN`;
- umbrales opcionales: `TEMP_MIN`, `TEMP_MAX`, `HISTERESIS`, `CONFIRMACIONES`, `SIN_DATOS_S`, `VALIDO_MIN`, `VALIDO_MAX`. Si no son coherentes (por ejemplo, `HISTERESIS` deja vacío el rango de vuelta a OK), el servicio no arranca y dice por qué.

Si se cambian los umbrales, hay que volver a correr `suscripciones.py`.

### 12.1 Despliegue

```bash
git pull
cp nube/.env.example nube/.env            # llenar SMTP_USER, SMTP_PASS, ALERTA_DESTINO, FRONT_URL y ALERTAS_TOKEN (openssl rand -hex 16)
```

1. **Antes de levantar nada, verificar que el Orion del servidor filtra bien sobre `Text`.** `orion:latest` puede ser de otra versión que la probada en local (4.5.0). Mandar `temperatura|X` con el `curl` de §2.5 y consultar `curl -sG localhost:1026/v2/entities -H fiware-service:openiot -H fiware-servicepath:/ --data-urlencode 'q=<q>'`. Si algo no coincide con esta tabla, **detenerse**:

   | X | q de "fuera" | q de "dentro" |
   |---|---|---|
   | 5 | aparece | — |
   | 25 | — | aparece |
   | 29 | — | — |
   | 35 | aparece | — |
   | 125 | — | — |

2. `cd nube && docker compose up -d --build alertas frontend`
3. `docker exec fiware-alertas python suscripciones.py`: deben salir 2 líneas `creada:`. Si se corre de nuevo, salen `actualizada:` y no se duplican. La suscripción de QuantumLeap no se toca.
4. Abrir el puerto **5050** en el security group (el navegador se conecta al websocket y a `/estado`).
5. Demo: `APIKEY=<apikey> ./alertas/demo.sh <servidor> 36` → correo de ALARMA y banner rojo. Después, `./alertas/demo.sh <servidor> 25` → correo de OK y banner verde.

| Demo | Resultado esperado |
|---|---|
| `demo.sh <servidor> 36` (3 tramas) | 1 correo ALARMA, banner rojo |
| `demo.sh <servidor> 36 5` | ningún correo nuevo |
| `demo.sh <servidor> 29` | nada cambia (banda) |
| `demo.sh <servidor> 25` | 1 correo OK, banner verde 5 s |
| Sin tramas > 2 min (placa apagada) | correo SIN DATOS, banner gris |
| `demo.sh <servidor> 125` | nada (lectura inválida) |

Si la placa real está encendida durante la demo, sus tramas se mezclan con las de prueba.
