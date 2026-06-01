#!/usr/bin/with-contenv bashio

BACKUP_DIR="/config/weburl_sms_login"
BACKUP_FILE="${BACKUP_DIR}/options_backup.json"
OPTIONS="/data/options.json"

# Persistent pip packages
export PYTHONPATH="/data/pip"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') INFO  $1"
}

log_error() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR $1" >&2
}

# Set timezone — try config override first, then HA system config
# Check for manual timezone override in config
MANUAL_TZ=$(jq -r '.timezone // ""' "$OPTIONS" 2>/dev/null)

if [ -n "$MANUAL_TZ" ] && [ -f "/usr/share/zoneinfo/$MANUAL_TZ" ]; then
    export TZ="$MANUAL_TZ"
    ln -sf "/usr/share/zoneinfo/$MANUAL_TZ" /etc/localtime
    echo "$MANUAL_TZ" > /etc/timezone 2>/dev/null || true
    log "Timezone set from config: $MANUAL_TZ"
elif [ -n "$SUPERVISOR_TOKEN" ]; then
    log "Detecting timezone from HA (SUPERVISOR_TOKEN present)..."
    HA_TZ=$(python3 -c "
import urllib.request, json, os, sys
token = os.environ.get('SUPERVISOR_TOKEN', '')
print('TOKEN_LEN=' + str(len(token)), file=sys.stderr)
try:
    req = urllib.request.Request('http://supervisor/core/api/config',
        headers={'Authorization': 'Bearer ' + token})
    resp = urllib.request.urlopen(req, timeout=5)
    data = json.loads(resp.read())
    tz = data.get('time_zone', '')
    print('API_RESPONSE_TZ=' + tz, file=sys.stderr)
    print(tz)
except Exception as e:
    print('TZ_ERROR: ' + str(e), file=sys.stderr)
" 2>&1)
    # HA_TZ now contains both stderr debug lines and the actual TZ on stdout
    # Extract just the timezone (last non-debug line)
    log "TZ detection output: $HA_TZ"
    CLEAN_TZ=$(echo "$HA_TZ" | grep -v '=' | grep -v 'TZ_ERROR' | tail -1)
    if [ -n "$CLEAN_TZ" ] && [ -f "/usr/share/zoneinfo/$CLEAN_TZ" ]; then
        export TZ="$CLEAN_TZ"
        ln -sf "/usr/share/zoneinfo/$CLEAN_TZ" /etc/localtime
        echo "$CLEAN_TZ" > /etc/timezone 2>/dev/null || true
        log "Timezone set to: $CLEAN_TZ"
    else
        log "Timezone detection failed (CLEAN_TZ='$CLEAN_TZ') — running in UTC"
    fi
else
    log "No SUPERVISOR_TOKEN — timezone cannot be detected, running in UTC"
fi

# Install selenium on first run (persists in /data/pip)
log "Checking dependencies..."
/app/first_run.sh
if [ $? -ne 0 ]; then
    log_error "Dependency install failed — check network and restart"
    sleep 300
    exit 1
fi

# Run config migration
log "Checking config schema..."
python3 /app/migrate.py

# Ensure backup dir exists
mkdir -p "$BACKUP_DIR"

# Back up current config to survive uninstalls
if [ -f "$OPTIONS" ]; then
    cp "$OPTIONS" "$BACKUP_FILE"
    log "Config backed up to ${BACKUP_FILE}"
fi

# Start Xvfb for headless Chromium (suppress xkbcomp warnings)
log "Starting Xvfb..."
Xvfb :99 -screen 0 1280x800x24 2>/dev/null &
sleep 1

CHROME_VER=$(chromium-browser --version 2>/dev/null || echo "unknown")
log "Chromium: $CHROME_VER"

log "Starting WebURL SMS Login..."

# Run Python directly — HTTP server + built-in cron scheduler
# No busybox crond needed. TZ, DISPLAY, SUPERVISOR_TOKEN all inherited.
exec python3 /app/weburl_sms_login.py --server --port 7900
