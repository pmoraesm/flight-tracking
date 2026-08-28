#!/usr/bin/env sh
set -e

echo "Generating config from Home Assistant options..."
python3 /app/ha_setup.py

export FLIGHT_DB_PATH=/data/prices.db

echo "Starting Flight Tracker..."
exec python3 /app/main.py
