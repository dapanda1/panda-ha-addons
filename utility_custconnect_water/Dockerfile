ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.22
FROM ${BUILD_FROM}

COPY app/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

COPY rootfs /
COPY app /app

RUN chmod a+x /etc/s6-overlay/s6-rc.d/utility-custconnect-water/run \
              /etc/s6-overlay/s6-rc.d/utility-custconnect-water/finish

ARG BUILD_ARCH
ARG BUILD_VERSION
LABEL \
    io.hass.name="Utility CustConnect Water" \
    io.hass.description="Hourly water usage from CustConnect utility portals" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.licenses="MIT"
