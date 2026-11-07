#!/usr/bin/with-contenv bashio
set -euo pipefail

# Mosquitto 6.5 leaves the Supervisor service empty and Bashio prints
# "Service not enabled". Hide that line. Version 7.1 registers the service.
bashio::log.level fatal
mqtt_ready=0
if bashio::services.available "mqtt"; then
    mqtt_ready=1
fi
bashio::log.level info
if [[ "${mqtt_ready}" -eq 1 ]]; then
    export MQTT_HOST="$(bashio::services mqtt "host")"
    export MQTT_PORT="$(bashio::services mqtt "port")"
    export MQTT_USER="$(bashio::services mqtt "username")"
    export MQTT_PASSWORD="$(bashio::services mqtt "password")"
    bashio::log.info "Using the Mosquitto service at ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.warning "Supervisor has no MQTT service. Update Mosquitto broker to 7.1 or newer, then restart this add-on. Until then core-mosquitto is used without the published username."
fi

export PYTHONPATH="/app"
exec python3 -m autoterm_flow5.main
