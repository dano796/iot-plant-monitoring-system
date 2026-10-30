#!/usr/bin/env bash
# Manda tramas de temperatura al IoT Agent para demostrar las alertas.
#   APIKEY=<apikey> ./demo.sh <host> <valor> [veces=3]
#   ej.: ./demo.sh <ec2> 36    -> ALARMA      ./demo.sh <ec2> 25 -> vuelve a OK
#        ./demo.sh <ec2> 29    -> banda, nada ./demo.sh <ec2> 125 -> invalida, se ignora
# PUERTO (5555) y DISPOSITIVO (plant001) se pueden cambiar por variable de entorno.
set -euo pipefail

HOST=${1:?uso: APIKEY=... $0 <host> <valor> [veces]}
VALOR=${2:?falta el valor de temperatura}
VECES=${3:-3}
: "${APIKEY:?falta la variable APIKEY}"
PUERTO=${PUERTO:-5555}
DISPOSITIVO=${DISPOSITIVO:-plant001}

for i in $(seq 1 "$VECES"); do
  codigo=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "http://$HOST:$PUERTO/iot/d?k=$APIKEY&i=$DISPOSITIVO" \
    -H "Content-Type: text/plain" -d "temperatura|$VALOR")
  echo "trama $i/$VECES temperatura|$VALOR -> HTTP $codigo"
  [ "$i" -lt "$VECES" ] && sleep 1
done
