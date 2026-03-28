ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 openssh-client

COPY listener.py /listener.py
COPY migrate.py /migrate.py
COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/plex-wol/run /etc/s6-overlay/s6-rc.d/plex-wol/finish
