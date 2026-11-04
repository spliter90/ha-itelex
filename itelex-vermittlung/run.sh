#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

if ! bashio::config.has_value 'oeffentliche_adresse'; then
    bashio::log.warning "Keine öffentliche Adresse eingetragen. Stationen 'über Vermittlung' sind dann nicht auffindbar."
fi

bashio::log.info "Starte i-Telex-Vermittlung ..."
cd /app || exit 1
exec python3 -u main.py
