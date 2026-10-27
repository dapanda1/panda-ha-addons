ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
FROM ${BUILD_FROM}

# Offline GeoIP: the DB-IP Lite country database (CC BY 4.0, https://db-ip.com),
# pinned and checksummed. To refresh it, set DBIP_VERSION to the latest release
# of the npm package @ip-location-db/dbip-country-mmdb and DBIP_SHA256 to the
# sha256 of that release's .tgz.
ARG DBIP_VERSION=2.3.2026060120
ARG DBIP_SHA256=5340f28d08f4e666473e0c5dc1c40d9db457ad00603d1f725fb4d8621c624239

RUN apk add --no-cache python3 py3-maxminddb openssh-client \
    && python3 -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
        "https://registry.npmjs.org/@ip-location-db/dbip-country-mmdb/-/dbip-country-mmdb-${DBIP_VERSION}.tgz" \
        /tmp/dbip.tgz \
    && echo "${DBIP_SHA256}  /tmp/dbip.tgz" | sha256sum -c - \
    && mkdir -p /tmp/dbip /usr/share/dbip \
    && tar -xzf /tmp/dbip.tgz -C /tmp/dbip package/dbip-country-ipv4.mmdb package/DBIP-LICENSE \
    && mv /tmp/dbip/package/dbip-country-ipv4.mmdb /tmp/dbip/package/DBIP-LICENSE /usr/share/dbip/ \
    && rm -rf /tmp/dbip /tmp/dbip.tgz

COPY listener.py /listener.py
COPY migrate.py /migrate.py
COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/plex-wol/run /etc/s6-overlay/s6-rc.d/plex-wol/finish
