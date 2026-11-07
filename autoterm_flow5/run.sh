#!/usr/bin/with-contenv bashio
set -euo pipefail

if bashio::services.available "mqtt"; then
    export MQTT_HOST="$(bashio::services mqtt "host")"
    export MQTT_PORT="$(bashio::services mqtt "port")"
    export MQTT_USER="$(bashio::services mqtt "username")"
    export MQTT_PASSWORD="$(bashio::services mqtt "password")"
fi

export PYTHONPATH="/app"
exec python3 -m autoterm_flow5.main
