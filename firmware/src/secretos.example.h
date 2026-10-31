// Plantilla de credenciales. Copiar a secretos.h y llenar: cp secretos.example.h secretos.h
#pragma once

#define WIFI_SSID   "mi_red"
#define WIFI_PASS   "mi_clave"
// IoT Agent UltraLight: k = apikey del grupo de servicio, i = id del dispositivo
#define AGENTE_URL  "http://<servidor>:5555/iot/d?k=<apikey>&i=plant001"
