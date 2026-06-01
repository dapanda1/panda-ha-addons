#!/bin/sh

# Install selenium and patch chromedriver to avoid PerimeterX detection.
# /data/ is persistent storage that survives container restarts.

PIP_DIR="/data/pip"
PATCHED_DRIVER="/data/chromedriver_patched"
export PYTHONPATH="$PIP_DIR"

# --- Step 1: Install selenium ---
python3 -c "import selenium" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "Selenium already installed"
else
    echo "Installing selenium to ${PIP_DIR}..."
    mkdir -p "$PIP_DIR"
    pip3 install --no-cache-dir --break-system-packages --root-user-action=ignore --target="$PIP_DIR" selenium==4.27.1
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to install selenium"
        exit 1
    fi
    echo "Selenium installed successfully"
fi

# --- Step 2: Patch chromedriver to remove cdc_ detection signatures ---
# PerimeterX scans the chromedriver binary for "cdc_" variable names.
# This is the primary detection vector that causes "Please try again."
HASH_FILE="/data/.chromedriver_hash"
CURRENT_HASH=$(md5sum /usr/bin/chromedriver | cut -d' ' -f1)
STORED_HASH=""
if [ -f "$HASH_FILE" ]; then
    STORED_HASH=$(cat "$HASH_FILE")
fi

if [ ! -f "$PATCHED_DRIVER" ] || [ "$CURRENT_HASH" != "$STORED_HASH" ]; then
    echo "Patching chromedriver (hash: $CURRENT_HASH)..."
    cp /usr/bin/chromedriver "$PATCHED_DRIVER"
    chmod +x "$PATCHED_DRIVER"

    python3 -c "
import re, random, string, sys

path = '$PATCHED_DRIVER'
with open(path, 'rb') as f:
    data = f.read()

# Find all cdc_ variable patterns and replace with random strings
count = 0
for match in set(re.findall(rb'cdc_[a-zA-Z0-9]{22}_', data)):
    replacement = b'xxx_' + ''.join(random.choices(string.ascii_lowercase, k=22)).encode() + b'_'
    data = data.replace(match, replacement)
    count += 1

with open(path, 'wb') as f:
    f.write(data)

print(f'Patched {count} cdc_ signature(s) in chromedriver')
"
    if [ $? -eq 0 ]; then
        echo "$CURRENT_HASH" > "$HASH_FILE"
    else
        echo "WARNING: chromedriver patch failed — using unpatched"
        rm -f "$PATCHED_DRIVER" "$HASH_FILE"
    fi
else
    echo "Chromedriver already patched"
fi
