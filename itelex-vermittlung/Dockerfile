# Aufbau nach https://contributors.home-assistant.io/apps/guides/dockerfile/
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-aiohttp tzdata

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

LABEL \
    org.opencontainers.image.title="Home Assistant App: i-Telex-Vermittlung" \
    org.opencontainers.image.description="Eigenes i-Telex-Teilnehmerverzeichnis und Vermittlung" \
    org.opencontainers.image.source="https://github.com/spliter90/ha-itelex" \
    org.opencontainers.image.licenses="MIT"
