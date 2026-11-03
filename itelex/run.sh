#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

if ! bashio::config.has_value 'kennung'; then
    bashio::log.warning "Keine Kennung gesetzt. Bitte in der Konfiguration eintragen (z. B. '12345 name d')."
fi

bashio::log.info "Starte i-Telex-Station ..."
cd /app || exit 1
exec python3 -u main.py
