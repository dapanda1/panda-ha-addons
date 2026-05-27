ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
FROM $BUILD_FROM

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir plexapi==4.16.1 aiohttp==3.10.5 \
    && rm -rf /root/.cache

COPY rootfs /
COPY server.py exporter.py migrate.py telegram_bot.py /usr/src/
COPY www/ /usr/src/www/

# Defensive: strip any CR characters that Windows may have introduced,
# then mark s6 scripts executable.
RUN find /etc/s6-overlay/s6-rc.d -type f -exec sed -i 's/\r$//' {} \; \
    && chmod a+x /etc/s6-overlay/s6-rc.d/server/run \
    && chmod a+x /etc/s6-overlay/s6-rc.d/init-migrate/up

LABEL \
  io.hass.name="Plex Library Index" \
  io.hass.description="Plex library exporter + ingress-served search page" \
  io.hass.type="addon" \
  io.hass.version="1.2.3" \
  io.hass.arch="aarch64"
