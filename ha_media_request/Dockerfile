ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
ARG BUILD_ARCH=amd64

FROM ghcr.io/home-assistant/aarch64-base:3.19 AS base-aarch64
FROM ghcr.io/home-assistant/amd64-base:3.19 AS base-amd64

FROM base-${BUILD_ARCH}

RUN apk add --no-cache python3

COPY media_requests.py /app/media_requests.py
COPY migrate.py /app/migrate.py
COPY rootfs /
