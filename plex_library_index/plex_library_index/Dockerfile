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

# Defensive: strip ALL carriage return bytes from s6 scripts and Python
# files. Uses `tr` which operates on bytes directly. Then verify the
# critical service scripts contain no CR — fail the build loudly if they
# somehow still do.
RUN set -e; \
    for f in $(find /etc/s6-overlay/s6-rc.d -type f) /usr/src/*.py; do \
        tr -d '\r' < "$f" > "$f.norm" && mv "$f.norm" "$f"; \
    done; \
    for f in /etc/s6-overlay/s6-rc.d/server/run /etc/s6-overlay/s6-rc.d/init-migrate/up; do \
        if od -c "$f" | grep -q '\\r'; then \
            echo "FATAL: $f still contains CR after normalization" >&2; \
            od -c "$f" | head -3 >&2; \
            exit 1; \
        fi; \
    done; \
    chmod a+x /etc/s6-overlay/s6-rc.d/server/run; \
    chmod a+x /etc/s6-overlay/s6-rc.d/init-migrate/up

LABEL \
  io.hass.name="Plex Library Index" \
  io.hass.description="Plex library exporter + ingress-served search page" \
  io.hass.type="addon" \
  io.hass.version="1.2.4" \
  io.hass.arch="aarch64"
