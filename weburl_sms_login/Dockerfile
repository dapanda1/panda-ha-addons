ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    python3 \
    py3-pip \
    chromium \
    chromium-chromedriver \
    nss \
    freetype \
    harfbuzz \
    ca-certificates \
    ttf-freefont \
    font-noto \
    font-noto-emoji \
    fontconfig \
    xvfb \
    jq \
    tzdata \
    && rm -rf /var/cache/apk/* \
    && fc-cache -f

ENV CHROMIUM_PATH=/usr/bin/chromium-browser
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

COPY weburl_sms_login.py /app/weburl_sms_login.py
COPY migrate.py /app/migrate.py
COPY first_run.sh /app/first_run.sh
COPY run.sh /

RUN chmod a+x /app/*.py /app/*.sh /run.sh

CMD [ "/run.sh" ]
