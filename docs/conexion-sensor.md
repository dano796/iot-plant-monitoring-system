# Checklist: conectar el sensor real

Pasos para montar la placa con los sensores reales y comprobar que los datos llegan a la nube.

## 1. Cableado (T-Beam V1.1)

| Sensor | Pin del sensor | Pin T-Beam |
|---|---|---|
| HDC1080 (temp/humedad) | SDA / SCL | 21 / 22 |
| AP3216 (luz) | SDA / SCL | 21 / 22 |
| HDC1080 y AP3216 | VCC / GND | 3V3 / GND |
| Humedad de suelo (módulo LM393) | AO | **GPIO36 (VP)** |
| Humedad de suelo | VCC / GND | **3V3** (nunca 5 V) / GND |
| Humedad de suelo | DO | **sin conectar** (el pin 4 es el LED de la placa) |
| GPS | — | integrado, solo necesita cielo abierto |

- [ ] **No usar GPIO0 ni GPIO4:** son ADC2 y no leen con el WiFi encendido.
- [ ] Medir con multímetro unos 3,3 V entre VCC y GND **en el pin del sensor**. La última vez los sensores I2C no respondían por cableado.

## 2. `firmware/src/secretos.h`

Copiar la plantilla y llenarla:

```bash
cp firmware/src/secretos.example.h firmware/src/secretos.h
```

```c
#define WIFI_SSID   "mi_red"
#define WIFI_PASS   "la_clave"
#define AGENTE_URL  "http://<servidor>:5555/iot/d?k=<apikey>&i=plant001"
```

`<apikey>` es la del grupo de servicio del IoT Agent. Este archivo está en `.gitignore`: nunca se sube.

## 3. Flashear y revisar el serial

```bash
cd firmware
pio run -t upload
pio device monitor
```

Al arrancar **no** deben aparecer estas líneas:
- `HDC1008 NO detectado en 0x40` → revisar SDA/SCL/VCC del HDC1080
- `AP3216 NO detectado en 0x1E` → revisar SDA/SCL/VCC del AP3216

En cada ciclo debería verse:
- `CHIRP` con T entre 15 y 35 °C aprox. Si sale **125** o negativa, el HDC1080 no responde.
- `L` distinta de 23592 lx. Si sale **23592**, el AP3216 no responde.
- `HTTP -> IoT Agent: 200`. Con `<0`, o con `WiFi sin conexion`, revisar `secretos.h` y la red.

## 4. Calibrar el sensor de suelo (`firmware/src/main.cpp`)

1. Con la sonda **al aire**, anotar `suelo raw=...` → va en `SUELO_SECO`.
2. Con la sonda **en un vaso de agua**, anotar `suelo raw=...` → va en `SUELO_MOJADO`.
3. Reemplazar los valores y volver a flashear:

```c
#define SUELO_SECO      4095   // <- valor al aire
#define SUELO_MOJADO    1500   // <- valor en agua
```

Si el valor raw no cambia entre aire y agua, revisar el cable de AO (debe ir a VP) y el VCC del módulo.

## 5. Verificar en la nube

| Qué | Dónde |
|---|---|
| Último dato (Orion) | `curl "http://<servidor>:1026/v2/entities?options=keyValues" -H "fiware-service: openiot" -H "fiware-servicepath: /"` |
| Dato limpio (API) | http://<servidor>:8000/api/plantas/ultimo |
| Dashboard | http://<servidor> |

El procesamiento corre cada 60 s. La API y el dashboard pueden tardar hasta un minuto en mostrar el dato.

Si el dato no llega a la API, revisar en el servidor:
```bash
docker logs --tail 20 fiware-iot-agent
docker logs --tail 5 fiware-processing
```

## Pendiente (no bloquea la prueba)

- [ ] Cerrar los puertos 4200, 4041, 1026 y 8668 en el firewall del servidor (no tienen autenticación).
- [ ] Borrar de Postgres las filas repetidas de antes del cambio del procesamiento.
