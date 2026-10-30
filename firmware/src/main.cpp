// Nodo sensor de la planta (LilyGO T-Beam V1.1, SX1276 a 915 MHz).
//
// Maquina de 3 estados que se repite cada PERIODO_TRAMA:
//   CHIRP    -> toma N_MUESTRAS de cada sensor (temperatura, humedad, suelo, luz) y lee el GPS
//   PRUNNING -> consolida cada variable con la mediana de las muestras
//   BUNDLING -> arma la trama UltraLight 2.0 y la envia por HTTP al IoT Agent (y por LoRa si el radio arranco)
//
// LoRaBoards.* y utilities.h son de LilyGO (MIT): inicializan la placa, la pantalla y el PMU.
#include <LoRa.h>
#include "LoRaBoards.h"
#include <ClosedCube_HDC1080.h>
#include <TinyGPSPlus.h>
#include <HTTPClient.h>
#include "secretos.h"   // WIFI_SSID, WIFI_PASS, AGENTE_URL (IoT Agent UL); no se sube a git, ver secretos.example.h

// ---- Radio LoRa (frecuencia en MHz, potencia en dBm, ancho de banda en kHz) ----
#ifndef CONFIG_RADIO_FREQ
#define CONFIG_RADIO_FREQ           915.0
#endif
#ifndef CONFIG_RADIO_OUTPUT_POWER
#define CONFIG_RADIO_OUTPUT_POWER   17
#endif
#ifndef CONFIG_RADIO_BW
#define CONFIG_RADIO_BW             125.0
#endif




#if !defined(USING_SX1276) && !defined(USING_SX1278)
#error "LoRa example is only allowed to run SX1276/78. For other RF models, please run examples/RadioLibExamples"
#endif


// ---- CHIRP / PRUNNING / BUNDLING ----
#define DEC_COORD       6          // decimales de lat/lon
#define DEC_TEMP        6          // decimales de temperatura/humedad
#define N_MUESTRAS      3          // prunning de al menos 3 muestras
#define T_RECUPERACION  1000       // ms entre chirps (HDC1008: conv 6.35/6.5ms, TI pide <1sps por autocalentamiento)
#define PERIODO_TRAMA   10000      // ms entre tramas

// Humedad de suelo: YL-69 + modulo LM393, salida AO.
// GPIO36 (VP) es ADC1: lee con el WiFi encendido (ADC2 = 0/2/4/12-15/25-27 no).
// Alimentar el modulo a 3V3: a 5V la salida AO puede pasar de 3.3V y danar el pin.
#define PIN_SUELO       36
#define SUELO_SECO      4095       // calibrar: lectura cruda con la sonda al aire
#define SUELO_MOJADO    1500       // calibrar: lectura cruda con la sonda en agua

// Luz: AP3216 (CJMCU-3216) por I2C
#define AP3216_ADDR     0x1E
#define AP3216_LUX      0.36f      // lux/cuenta en el rango por defecto (0-20661 lux)

enum Estado { CHIRP, PRUNNING, BUNDLING };
Estado estado = CHIRP;

float muestrasT[N_MUESTRAS], muestrasH[N_MUESTRAS], muestrasS[N_MUESTRAS], muestrasL[N_MUESTRAS];
float temperaturaFinal, humedadFinal, sueloFinal, luzFinal;
double lat, lon;
String trama;
unsigned long tCiclo;
bool loraOk = false;   // si el radio falla se sigue enviando por WiFi

// consolidacion: mediana de 3 (descarta el outlier, no lo promedia)
static float mediana3(float a, float b, float c)
{
  return max(min(a, b), min(max(a, b), c));
}

static uint8_t ap3216Leer(uint8_t reg)
{
  Wire.beginTransmission(AP3216_ADDR);
  Wire.write(reg);
  Wire.endTransmission();
  Wire.requestFrom(AP3216_ADDR, 1);
  return Wire.read();
}

static float leerLux()
{
  uint16_t raw = ap3216Leer(0x0C) | (ap3216Leer(0x0D) << 8);   // ALS data low/high
  return raw * AP3216_LUX;
}

// % de humedad de suelo: el sensor da MAS voltaje cuanto MAS seco
static float leerSuelo()
{
  int raw = analogRead(PIN_SUELO);
  Serial.printf("  suelo raw=%d\n", raw);   // para calibrar SUELO_SECO / SUELO_MOJADO
  return constrain((SUELO_SECO - raw) * 100.0f / (SUELO_SECO - SUELO_MOJADO), 0.0f, 100.0f);
}

int counter = 0;   // tramas enviadas (se muestra en la pantalla)

TinyGPSPlus gps;                      // parser NMEA del GPS integrado
ClosedCube_HDC1080 sensor;            // temperatura y humedad del aire (I2C 0x40)
HardwareSerial comunicacion_gps(1);   // UART1 conectada al GPS


// Espera ms milisegundos sin dejar de alimentar al parser del GPS
static void smartDelay(unsigned long ms)                
{
  unsigned long start = millis();
  do
  {
    while (comunicacion_gps.available())
      gps.encode(comunicacion_gps.read());
  } while (millis() - start < ms);
}

void setup()
{
    
    setupBoards();
    WiFi.mode(WIFI_STA);   // setupBoards levanta un AP que no se usa; pasamos a cliente
    WiFi.begin(WIFI_SSID, WIFI_PASS);   // reconecta solo si se cae
    comunicacion_gps.begin(9600, SERIAL_8N1, 34, 12);   // RX ESP32=34 (TX del GPS), TX ESP32=12
    // al encender, el GPS necesita un momento antes de leer los sensores
    smartDelay(1500);
    sensor.begin(0x40);
    if (sensor.readManufacturerId() != 0x5449)   // 0x5449 = "TI"; sin sensor se leeria T=125 H=100
        Serial.println("HDC1008 NO detectado en 0x40, revisar cableado SDA21/SCL22");
    Wire.beginTransmission(AP3216_ADDR);
    Wire.write(0x00); Wire.write(0x01);          // System Config = ALS activo
    if (Wire.endTransmission() != 0)
        Serial.println("AP3216 NO detectado en 0x1E");
    smartDelay(1500);

#ifdef  RADIO_TCXO_ENABLE
    pinMode(RADIO_TCXO_ENABLE, OUTPUT);
    digitalWrite(RADIO_TCXO_ENABLE, HIGH);
#endif

    // autoprueba de la mediana: imprime "mediana OK" en el serial
    Serial.println(mediana3(3, 1, 2) == 2 && mediana3(1, 99, 2) == 2 ? "mediana OK" : "mediana FAIL");

    Serial.println("LoRa Sender");
    LoRa.setPins(RADIO_CS_PIN, RADIO_RST_PIN, RADIO_DIO0_PIN);
    loraOk = LoRa.begin(CONFIG_RADIO_FREQ * 1000000);
    if (!loraOk) {
        Serial.println("Starting LoRa failed! se sigue solo por WiFi");
        return;   // la configuracion del radio es lo ultimo de setup
    }

    LoRa.setTxPower(CONFIG_RADIO_OUTPUT_POWER);

    LoRa.setSignalBandwidth(CONFIG_RADIO_BW * 1000);

    LoRa.setSpreadingFactor(10);

    LoRa.setPreambleLength(16);

    LoRa.setSyncWord(0xAB);

    LoRa.disableCrc();

    LoRa.disableInvertIQ();

    LoRa.setCodingRate4(7);
}

void loop()
{
    switch (estado) {

    case CHIRP:  // toma de medidas
        tCiclo = millis();
        for (int i = 0; i < N_MUESTRAS; i++) {
            muestrasT[i] = sensor.readTemperature();
            muestrasH[i] = sensor.readHumidity();
            muestrasS[i] = leerSuelo();
            muestrasL[i] = leerLux();
            Serial.printf("CHIRP %d -> T=%.2f H=%.2f S=%.1f%% L=%.1flx\n", i, muestrasT[i], muestrasH[i], muestrasS[i], muestrasL[i]);
            smartDelay(T_RECUPERACION);   // tiempo de recuperacion del sensor + lectura GPS
        }
        if (gps.location.isValid()) {
            lat = gps.location.lat();
            lon = gps.location.lng();
        } else {
            // chars=0 -> no llega NMEA (cableado/alimentacion); chars>0 -> solo falta cielo abierto
            Serial.printf("GPS sin fix (sats=%u, chars=%lu), se envia ultima posicion conocida\n",
                          gps.satellites.value(), gps.charsProcessed());
        }
        estado = PRUNNING;
        break;

    case PRUNNING:  // consolidacion de la medida
        temperaturaFinal = mediana3(muestrasT[0], muestrasT[1], muestrasT[2]);
        humedadFinal     = mediana3(muestrasH[0], muestrasH[1], muestrasH[2]);
        sueloFinal       = mediana3(muestrasS[0], muestrasS[1], muestrasS[2]);
        luzFinal         = mediana3(muestrasL[0], muestrasL[1], muestrasL[2]);
        Serial.printf("PRUNNING -> T=%.2f H=%.2f S=%.1f%% L=%.1flx\n", temperaturaFinal, humedadFinal, sueloFinal, luzFinal);
        estado = BUNDLING;
        break;

    case BUNDLING: {  // conformacion y envio de la trama
        // UltraLight 2.0 (clave|valor|...) para el IoT Agent de la nube
        trama  = "temperatura|"   + String(temperaturaFinal, DEC_TEMP);
        trama += "|humedad|"      + String(humedadFinal, DEC_TEMP);
        trama += "|humedadSuelo|" + String(sueloFinal, DEC_TEMP);
        trama += "|luz|"          + String(luzFinal, DEC_TEMP);
        trama += "|lat|"          + String(lat, DEC_COORD);
        trama += "|lon|"          + String(lon, DEC_COORD);
        Serial.println("BUNDLING -> " + trama);

        if (loraOk) {
            LoRa.beginPacket();
            LoRa.print(trama);
            LoRa.endPacket();
        }

        if (WiFi.status() == WL_CONNECTED) {
            HTTPClient http;
            http.begin(AGENTE_URL);
            http.addHeader("Content-Type", "text/plain");
            Serial.printf("HTTP -> IoT Agent: %d\n", http.POST(trama));   // 200 ok, <0 sin conexion
            http.end();
        } else {
            Serial.println(loraOk ? "WiFi sin conexion, trama solo por LoRa" : "WiFi sin conexion y sin LoRa, trama perdida");
        }

        if (u8g2) {
            char buf[32];
            u8g2->clearBuffer();
            u8g2->drawStr(0, 12, "Transmitting: OK!");
            snprintf(buf, sizeof(buf), "T:%.1f #%d", temperaturaFinal, counter);
            u8g2->drawStr(0, 30, buf);
            u8g2->sendBuffer();
        }
        counter++;
        unsigned long transcurrido = millis() - tCiclo;
        smartDelay(transcurrido < PERIODO_TRAMA ? PERIODO_TRAMA - transcurrido : 0);
        estado = CHIRP;
        break;
    }
    }
}
