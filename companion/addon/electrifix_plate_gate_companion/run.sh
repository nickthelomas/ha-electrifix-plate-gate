#!/usr/bin/with-contenv bashio
# Reads the add-on options; if the MQTT fields are blank, borrows the MQTT add-on's service login.
set -e
export PYTHONPATH=/app PYTHONUNBUFFERED=1   # s6 starts us with a clean environment
export PG_FRIGATE_MEDIA_DIR="$(bashio::config 'frigate_media_dir')"
export PG_DEPLOYMENT=haos
export PG_COMPANION_ID=addon
export PG_FRIGATE_CONFIG_DIR="$(bashio::config 'frigate_config_dir')"
export PG_ALLOWED_BASES="$(bashio::config 'allowed_bases')"
if bashio::config.has_value 'mqtt_host'; then
  export PG_MQTT_HOST="$(bashio::config 'mqtt_host')"
  export PG_MQTT_PORT="$(bashio::config 'mqtt_port')"
  export PG_MQTT_USER="$(bashio::config 'mqtt_user')"
  export PG_MQTT_PASS="$(bashio::config 'mqtt_password')"
elif bashio::services.available 'mqtt'; then
  export PG_MQTT_HOST="$(bashio::services mqtt 'host')"
  export PG_MQTT_PORT="$(bashio::services mqtt 'port')"
  export PG_MQTT_USER="$(bashio::services mqtt 'username')"
  export PG_MQTT_PASS="$(bashio::services mqtt 'password')"
else
  bashio::log.error "No MQTT details: install the Mosquitto add-on, or fill in mqtt_host in this add-on's options."
  exit 1
fi
bashio::log.info "Plate Gate Companion: Frigate config at ${PG_FRIGATE_CONFIG_DIR}, MQTT ${PG_MQTT_HOST}:${PG_MQTT_PORT}"
exec /opt/venv/bin/python3 -m plate_gate_companion
