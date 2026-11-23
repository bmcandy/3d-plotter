#!/usr/bin/with-contenv bashio
set -e

export SERIAL_PORT=$(bashio::config 'serial_port')
if [ -z "$SERIAL_PORT" ]; then
    # first detected serial device; the CR-10s shows up as ttyACM* or ttyUSB*
    SERIAL_PORT=$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1 || true)
fi
if [ -n "$SERIAL_PORT" ]; then
    bashio::log.info "Using serial port: $SERIAL_PORT"
    export SERIAL_PORT
else
    bashio::log.warning "No serial device found; plotting will fail until one is attached"
fi

if bashio::config.true 'simulate'; then
    export CREALIPEN_SIMULATE=1
fi

exec /opt/venv/bin/python -m uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8099
