#!/usr/bin/env python3
"""
WebURL SMS Login — HAOS Add-on (Selenium)
Logs into TextNow via headless Chromium, sends an SMS to a configured
recipient, cycling through up to 5 pre-defined messages on a cron schedule.
"""

import email as email_lib
import imaplib
import json
import logging
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "/usr/bin/chromium-browser")
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

# --- Timezone: ensure TZ is set before any logging ---
# run.sh should set TZ, but as a fallback, detect it here in Python too.
def _detect_timezone():
    """Query HA Supervisor API for the system timezone."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "http://supervisor/core/api/config",
            headers={"Authorization": f"Bearer {token}"})
        data = json.loads(urllib.request.urlopen(req, timeout=5).read())
        return data.get("time_zone", "")
    except Exception:
        return None

if not os.environ.get("TZ") or os.environ.get("TZ") == "UTC":
    # Check config option first
    try:
        with open("/data/options.json") as f:
            _opts = json.load(f)
        _cfg_tz = _opts.get("timezone", "")
        if _cfg_tz and os.path.isfile(f"/usr/share/zoneinfo/{_cfg_tz}"):
            os.environ["TZ"] = _cfg_tz
            time.tzset()
    except Exception:
        pass

    # If still not set, try Supervisor API
    if not os.environ.get("TZ") or os.environ.get("TZ") == "UTC":
        _tz = _detect_timezone()
        if _tz and os.path.isfile(f"/usr/share/zoneinfo/{_tz}"):
            os.environ["TZ"] = _tz
            time.tzset()

# Final tzset to pick up whatever TZ is set
if os.environ.get("TZ"):
    time.tzset()

CONFIG_DIR = Path("/config/weburl_sms_login")
OPTIONS_PATH = Path("/data/options.json")
BACKUP_PATH = CONFIG_DIR / "options_backup.json"
STATE_PATH = CONFIG_DIR / "state.json"
COOKIE_PATH = CONFIG_DIR / "cookies.json"
LOG_DIR = CONFIG_DIR / "screenshots"
HISTORY_PATH = CONFIG_DIR / "history.json"
LOCK_PATH = CONFIG_DIR / "run.lock"
MAX_HISTORY = 200

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("weburl_sms_login")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

def load_config():
    if OPTIONS_PATH.exists():
        with open(OPTIONS_PATH) as f:
            cfg = json.load(f)
    elif BACKUP_PATH.exists():
        log.info("Loading config from backup")
        with open(BACKUP_PATH) as f:
            cfg = json.load(f)
    else:
        raise FileNotFoundError("No config found")
    messages = []
    for i in range(1, 6):
        msg = cfg.pop(f"message_{i}", "")
        if msg and msg.strip():
            messages.append(msg.strip())
    cfg["messages"] = messages if messages else cfg.get("messages", [])
    selectors = {}
    for key in ("new_message", "recipient", "message_input", "send_button"):
        val = cfg.pop(f"selector_{key}", "")
        if val and val.strip():
            selectors[key] = val.strip()
    cfg["selectors"] = selectors if selectors else cfg.get("selectors", {})
    if not cfg.get("imap_user"):
        cfg["imap_user"] = cfg.get("textnow_email", "")

    # Strip invisible Unicode characters from phone number (common when pasting)
    num = cfg.get("send_to_number", "")
    if num:
        cfg["send_to_number"] = re.sub(r'[^\+\d]', '', num)

    return cfg

def notify_ha(title, message, nid="weburl_sms_login"):
    if not SUPERVISOR_TOKEN: return
    url = "http://supervisor/core/api/services/persistent_notification/create"
    payload = json.dumps({"title": title, "message": message, "notification_id": nid}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {SUPERVISOR_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=10)
        log.info(f"HA notification sent: {title}")
    except Exception as e:
        log.warning(f"HA notification failed: {e}")

def notify_mobile(cfg, title, message):
    device = cfg.get("notify_mobile_device", "")
    if not device or not SUPERVISOR_TOKEN: return
    url = f"http://supervisor/core/api/services/notify/{device}"
    payload = json.dumps({"title": title, "message": message}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {SUPERVISOR_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try: urllib.request.urlopen(req, timeout=10)
    except Exception as e: log.warning(f"Mobile notification failed: {e}")

def send_notifications(cfg, success, detail):
    if success:
        title = cfg.get("notify_title_success", "WebURL SMS Login — Sent")
        msg = cfg.get("notify_message_success", "SMS sent successfully.") + f"\n{detail}"
    else:
        title = cfg.get("notify_title_failure", "WebURL SMS Login — Failed")
        msg = cfg.get("notify_message_failure", "SMS sending failed.") + f"\n{detail}"
    notify_ha(title, msg)
    notify_mobile(cfg, title, msg)

def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f: return json.load(f)
    return {"message_index": 0, "send_count": 0, "last_sent": None}

def save_state(state):
    with open(STATE_PATH, "w") as f: json.dump(state, f, indent=2)

def get_next_message(cfg):
    messages = cfg["messages"]
    state = load_state()
    idx = state["message_index"] % len(messages)
    log.info(f"Message {idx + 1}/{len(messages)}: {messages[idx]}")
    return messages[idx]

def confirm_message_sent(cfg):
    messages = cfg["messages"]
    state = load_state()
    state["message_index"] = (state["message_index"] % len(messages)) + 1
    state["send_count"] = state.get("send_count", 0) + 1
    state["last_sent"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)

def render_message(template):
    import datetime
    now = datetime.datetime.now()
    r = {"{date}": now.strftime("%Y-%m-%d"), "{time}": now.strftime("%-I:%M %p"),
         "{time24}": now.strftime("%H:%M"), "{day}": now.strftime("%A"),
         "{day_short}": now.strftime("%a"), "{month}": now.strftime("%B"),
         "{month_short}": now.strftime("%b"), "{month_num}": now.strftime("%m"),
         "{year}": now.strftime("%Y"), "{day_num}": now.strftime("%-d"),
         "{hour}": now.strftime("%-I"), "{hour24}": now.strftime("%H"),
         "{minute}": now.strftime("%M"), "{weekday_num}": str(now.weekday())}
    result = template
    for k, v in r.items():
        result = result.replace(k, v)
    return result

def get_selectors(cfg, key, defaults):
    custom = cfg.get("selectors", {}).get(key, "")
    if custom:
        log.info(f"Custom selector for {key}: {custom}")
        return [custom] + defaults
    return defaults

def fetch_verification_code(cfg):
    imap_user = cfg.get("imap_user", cfg["textnow_email"])
    imap_pass = cfg["imap_app_password"]
    imap_server = cfg.get("imap_server", "imap.gmail.com")
    imap_port = cfg.get("imap_port", 993)
    sender_filter = cfg.get("sender_filter", "textnow")
    timeout = cfg.get("code_timeout", 120)
    poll_interval = cfg.get("code_poll_interval", 5)
    log.info(f"Polling {imap_server}:{imap_port} for code (timeout={timeout}s)")
    start = time.time()
    seen = set()
    imap_timeout = min(30, poll_interval * 2)  # socket-level timeout
    while time.time() - start < timeout:
        try:
            mail = imaplib.IMAP4_SSL(imap_server, imap_port, timeout=imap_timeout)
            mail.login(imap_user, imap_pass)
            mail.select("INBOX")
            _, ids = mail.search(None, "UNSEEN")
            if ids[0]:
                for mid in ids[0].split():
                    ms = mid.decode()
                    if ms in seen: continue
                    _, data = mail.fetch(mid, "(RFC822)")
                    msg = email_lib.message_from_bytes(data[0][1])
                    frm = msg.get("From", "").lower()
                    subj = msg.get("Subject", "").lower()
                    if sender_filter.lower() not in frm and sender_filter.lower() not in subj:
                        seen.add(ms); continue
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() in ("text/plain", "text/html"):
                                body = part.get_payload(decode=True).decode(errors="replace"); break
                    else:
                        body = msg.get_payload(decode=True).decode(errors="replace")
                    for pat in [r"(?:code|pin|otp|verification)[:\s]+(\d{4,8})",
                                r"(\d{4,8})\s*(?:is your|verification|code)", r"\b(\d{6})\b"]:
                        m = re.search(pat, body, re.IGNORECASE)
                        if m:
                            code = m.group(1)
                            log.info(f"Found code: {code}")
                            mail.logout()
                            return code
                    seen.add(ms)
            mail.logout()
        except Exception as e:
            log.warning(f"IMAP error: {e}")
        log.info(f"No code yet — {poll_interval}s ({int(time.time()-start)}s)")
        time.sleep(poll_interval)
    log.error("Timed out waiting for code")
    return None

# --- Selenium helpers ---

PATCHED_CHROMEDRIVER = "/data/chromedriver_patched"

def create_driver(cfg=None, debug_port=None):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    opts = Options()
    opts.binary_location = CHROMIUM_PATH

    # --- Core flags ---
    # No --headless: runs against Xvfb virtual display instead.
    # Xvfb is undetectable; headless mode has fingerprint differences.
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-translate")
    opts.add_argument("--disable-sync")

    # --- Remote debugging (for manual CAPTCHA) ---
    if debug_port:
        opts.add_argument(f"--remote-debugging-port={debug_port}")
        opts.add_argument("--remote-debugging-address=0.0.0.0")
        log.info(f"Chrome remote debugging enabled on port {debug_port}")

    # --- Anti-detection flags ---
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--allow-running-insecure-content")
    opts.add_argument("--lang=en-US,en")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # Configurable Chrome version for user-agent
    chrome_ver = cfg.get("chrome_version", "146") if cfg else "146"
    user_agent = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_ver}.0.0.0 Safari/537.36"
    )
    opts.add_argument(f"--user-agent={user_agent}")
    log.info(f"User-agent: Chrome/{chrome_ver}")

    # Use patched chromedriver if available (cdc_ signatures removed)
    driver_path = PATCHED_CHROMEDRIVER if os.path.isfile(PATCHED_CHROMEDRIVER) else CHROMEDRIVER_PATH
    log.info(f"Using chromedriver: {driver_path}")
    svc = Service(executable_path=driver_path)
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.set_page_load_timeout(30)
    driver.implicitly_wait(5)

    # Comprehensive stealth JS — injected before every page load
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        // 1. Remove webdriver flag
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        // Also delete it from the prototype
        delete navigator.__proto__.webdriver;

        // 2. Fake plugins array (headless has 0)
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const plugins = [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
                      description: 'Portable Document Format', length: 1 },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                      description: '', length: 1 },
                    { name: 'Native Client', filename: 'internal-nacl-plugin',
                      description: '', length: 2 }
                ];
                plugins.length = 3;
                return plugins;
            }
        });

        // 3. Fake mimeTypes
        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => {
                const mimes = [
                    { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
                    { type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format' }
                ];
                mimes.length = 2;
                return mimes;
            }
        });

        // 4. Languages
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

        // 5. Platform
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

        // 6. Hardware concurrency (headless often returns unusual values)
        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

        // 7. Device memory
        Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

        // 8. Connection (headless Chrome often lacks this)
        if (!navigator.connection) {
            Object.defineProperty(navigator, 'connection', {
                get: () => ({ effectiveType: '4g', rtt: 50, downlink: 10, saveData: false })
            });
        }

        // 9. Chrome runtime object
        window.chrome = {
            runtime: { id: undefined, connect: function(){}, sendMessage: function(){} },
            loadTimes: function() { return {} },
            csi: function() { return {} }
        };

        // 10. Permissions API fix
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : origQuery(params);

        // 11. WebGL vendor/renderer (headless has 'Google Inc.' / 'ANGLE...')
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {
            if (param === 37445) return 'Intel Inc.';           // VENDOR
            if (param === 37446) return 'Intel Iris OpenGL Engine'; // RENDERER
            return getParameter.call(this, param);
        };

        // 12. Notification permission (headless defaults to 'denied')
        Object.defineProperty(Notification, 'permission', { get: () => 'default' });

        // 13. outerWidth/outerHeight (headless often has 0)
        if (window.outerWidth === 0) {
            Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
            Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });
        }

        // 14. Screen properties
        Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
        Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
    """})

    return driver

def find_click(driver, selectors, desc="element"):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    for sel in selectors:
        try:
            el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            el.click()
            log.info(f"Clicked {desc}: {sel}")
            return True
        except Exception: continue
    xpaths = {"Continue with Email": "//button[contains(.,'Continue with Email')]|//*[contains(text(),'Continue with Email')]",
              "Continue": "//button[contains(.,'Continue')]|//button[@type='submit']",
              "Verify": "//button[contains(.,'Verify')]|//button[contains(.,'Continue')]|//button[@type='submit']",
              "Send": "//button[contains(.,'Send')]|//button[@aria-label='Send']",
              "New": "//button[contains(.,'New')]|//button[@aria-label='New message']"}
    for text, xp in xpaths.items():
        if text.lower() in desc.lower():
            try:
                el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, xp)))
                el.click()
                log.info(f"Clicked {desc} via XPath")
                return True
            except Exception: continue
    log.warning(f"Could not find {desc}")
    return False

def find_fill(driver, selectors, value, desc="field"):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    for sel in selectors:
        try:
            el = WebDriverWait(driver, 3).until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
            el.clear(); el.send_keys(value)
            log.info(f"Filled {desc}: {sel}")
            return el
        except Exception: continue
    log.warning(f"Could not find {desc}")
    return None

def shot(driver, name):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = str(LOG_DIR / name)
        driver.save_screenshot(path)
        log.info(f"Screenshot: {path}")
    except Exception as e:
        log.warning(f"Screenshot failed ({name}): {e}")

def handle_captcha(driver, cfg=None, attempt=1):
    """
    Handle PerimeterX CAPTCHA on TextNow.

    Structure: div#px-captcha → closed shadow root → iframe → #document → div[role=button]
    Selenium cannot access closed shadow roots. Instead, we click-and-hold at
    the coordinates of #px-captcha — browser delivers mouse events to whatever
    is rendered at those coordinates regardless of shadow DOM boundaries.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.action_chains import ActionChains

    p = f"captcha_a{attempt}_"  # prefix for screenshot filenames

    log.info("Checking for CAPTCHA...")
    log.info(f"Current URL: {driver.current_url}")
    shot(driver, f"{p}pre.png")

    # --- Step 1: Find the PerimeterX CAPTCHA container ---
    px_elements = driver.find_elements(By.CSS_SELECTOR, "#px-captcha")
    if not px_elements:
        # Secondary detection: check for captcha text + alternative container IDs
        captcha_texts = driver.find_elements(By.XPATH,
            "//*[contains(text(),'Verify you are a human')]")
        if captcha_texts:
            log.info("Found 'Verify you are a human' but no #px-captcha")
            px_elements = driver.find_elements(By.CSS_SELECTOR,
                "[id*='captcha'], [class*='captcha'], [id*='px-']")
            if px_elements:
                log.info(f"Alternative captcha container: <{px_elements[0].tag_name}> "
                         f"id='{px_elements[0].get_attribute('id')}'")

    if not px_elements:
        log.info("No CAPTCHA detected")
        return False

    px = px_elements[0]
    rect = px.rect
    log.info(f"PerimeterX CAPTCHA: #px-captcha at ({rect['x']},{rect['y']}) "
             f"size {rect['width']}x{rect['height']}")
    shot(driver, f"{p}found.png")

    # --- Step 2: Click-and-hold with human-like mouse movement ---
    import random

    # Random offset within the button area (not dead center)
    x_off = random.randint(-30, 30)
    y_off = random.randint(5, 20)
    log.info(f"Click target: center of #px-captcha + offset ({x_off}, {y_off})")

    try:
        # Move to page body first, then drift toward the button
        body = driver.find_element(By.TAG_NAME, "body")
        actions = ActionChains(driver)
        actions.move_to_element_with_offset(body, rect['x'] - 100, rect['y'] - 50)
        actions.pause(random.uniform(0.1, 0.3))
        actions.move_to_element_with_offset(px, x_off, y_off)
        actions.pause(random.uniform(0.2, 0.5))
        actions.click_and_hold()
        actions.perform()
    except Exception as e:
        log.warning(f"Human-like click failed: {e} — trying simple click")
        try:
            ActionChains(driver).move_to_element(px).click_and_hold().perform()
        except Exception as e2:
            log.error(f"Click-and-hold failed: {e2}")
            shot(driver, f"{p}click_failed.png")
            return False

    shot(driver, f"{p}holding.png")

    # --- Step 3: Hold for a randomized duration, then check ---
    import random
    hold_base = cfg.get("captcha_hold_base", 10) if cfg else 10
    hold_random_max = cfg.get("captcha_hold_random", 10) if cfg else 10
    post_wait = cfg.get("captcha_post_wait", 5) if cfg else 5
    hold_duration = hold_base + random.uniform(0, hold_random_max)
    log.info(f"Holding for {hold_duration:.1f}s (base={hold_base}, random=0-{hold_random_max}, post_wait={post_wait})")
    start_hold = time.time()
    while time.time() - start_hold < hold_duration:
        elapsed = time.time() - start_hold
        if int(elapsed) % 3 == 0 and int(elapsed) > 0:
            log.info(f"  holding... {int(elapsed)}s")
        time.sleep(random.uniform(0.8, 1.2))

    actual_held = round(time.time() - start_hold, 1)
    log.info(f"Hold complete — {actual_held}s elapsed, releasing")

    try:
        ActionChains(driver).release().perform()
    except Exception as e:
        log.warning(f"Release failed: {e}")

    shot(driver, f"{p}after_release.png")
    log.info(f"Waiting {post_wait}s for CAPTCHA to process...")
    time.sleep(post_wait)

    # --- Step 4: Check if CAPTCHA is gone ---
    success = False
    try:
        px_check = driver.find_elements(By.CSS_SELECTOR, "#px-captcha")
        if not px_check:
            log.info("px-captcha gone — CAPTCHA passed")
            success = True
        else:
            style = px_check[0].get_attribute("style") or ""
            height = px_check[0].rect.get('height', 999)
            log.info(f"px-captcha still present: style='{style[:80]}' height={height}")
            if "display: none" in style or "display:none" in style or height < 10:
                log.info("px-captcha hidden/collapsed — CAPTCHA passed")
                success = True

        if not success:
            if "/messaging" in driver.current_url.lower():
                log.info("Page navigated to messaging — CAPTCHA passed")
                success = True
    except Exception as e:
        log.warning(f"Post-hold check error: {e}")

    shot(driver, f"{p}after.png")

    if success:
        log.info("CAPTCHA handling complete — success")
    else:
        log.warning("CAPTCHA may still be present — check captcha_after.png")
    return success

def save_cookies(driver):
    with open(COOKIE_PATH, "w") as f: json.dump(driver.get_cookies(), f)
    log.info(f"Cookies saved: {len(driver.get_cookies())} cookies to {COOKIE_PATH}")

def load_cookies(driver):
    if not COOKIE_PATH.exists(): return False
    try:
        with open(COOKIE_PATH) as f: cookies = json.load(f)
        log.info(f"Loading {len(cookies)} cookies from {COOKIE_PATH}")
        driver.get("https://www.textnow.com")
        time.sleep(2)

        loaded = 0
        now = time.time()
        for c in cookies:
            name = c.get("name", "?")

            # Check expiration before processing
            exp = c.get("expirationDate") or c.get("expiry")
            if exp and float(exp) < now:
                log.info(f"Cookie expired, skipping: {name} (expired {int(now - float(exp))}s ago)")
                continue

            # Preserve hostOnly before building the Selenium cookie
            host_only = c.get("hostOnly", False)

            # Build a clean cookie dict with only fields Selenium accepts:
            # name, value, domain, path, secure, httpOnly, expiry, sameSite
            sc = {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
            }
            if "domain" in c:
                d = c["domain"]
                # Only add dot prefix for non-hostOnly cookies
                if not host_only and not d.startswith("."):
                    d = "." + d
                sc["domain"] = d
            if "path" in c:
                sc["path"] = c["path"]
            if "secure" in c:
                sc["secure"] = c["secure"]
            if "httpOnly" in c:
                sc["httpOnly"] = c["httpOnly"]

            # Convert expirationDate (float) to expiry (int)
            if "expirationDate" in c:
                sc["expiry"] = int(c["expirationDate"])
            elif "expiry" in c:
                sc["expiry"] = int(c["expiry"])

            # Convert sameSite to Selenium format
            ss = c.get("sameSite")
            if ss:
                ss_map = {"no_restriction": "None", "lax": "Lax", "strict": "Strict",
                          "none": "None", "None": "None", "Lax": "Lax", "Strict": "Strict"}
                if ss in ss_map:
                    sc["sameSite"] = ss_map[ss]

            try:
                driver.add_cookie(sc)
                loaded += 1
                log.info(f"  + {name} (domain={sc.get('domain')} ho={host_only} secure={sc.get('secure')} httpOnly={sc.get('httpOnly')})")
            except Exception as e:
                log.warning(f"Cookie skip ({name}): {e}")
        log.info(f"Loaded {loaded}/{len(cookies)} cookies")
        return loaded > 0
    except Exception as e:
        log.warning(f"Cookie load error: {e}")
        return False

def full_login(driver, cfg):
    tn_email = cfg["textnow_email"]
    log.info("=== Full login ===")
    driver.get("https://www.textnow.com/login")
    time.sleep(3); shot(driver, "step1_login.png")
    if not find_click(driver, ["button[data-testid*='email']", "[class*='email']"], "Continue with Email"):
        shot(driver, "error_no_email_btn.png"); return False
    time.sleep(2)
    el = find_fill(driver, ["input[type='email']", "input[name='email']", "input[placeholder*='email' i]", "input"], tn_email, "email")
    if not el: shot(driver, "error_no_email.png"); return False
    find_click(driver, ["button[type='submit']"], "Continue")
    time.sleep(2); shot(driver, "step2_email.png")

    # CAPTCHA retry — PerimeterX shows "Please try again" on failure
    captcha_passed = False
    for captcha_attempt in range(1, 4):  # up to 3 attempts
        log.info(f"CAPTCHA attempt {captcha_attempt}/3")
        captcha_passed = handle_captcha(driver, cfg, attempt=captcha_attempt)
        if captcha_passed:
            break
        log.warning(f"CAPTCHA attempt {captcha_attempt} failed")
        time.sleep(2)
        # Check if "Please try again" is shown — widget is ready for retry
        from selenium.webdriver.common.by import By as _By
        retry_els = driver.find_elements(_By.CSS_SELECTOR, "#px-captcha")
        if not retry_els:
            log.info("px-captcha gone after failed attempt — may have passed")
            captcha_passed = True
            break
        shot(driver, f"captcha_retry_{captcha_attempt}.png")

    time.sleep(2); shot(driver, "step3_captcha.png")
    if not captcha_passed:
        log.error("CAPTCHA not cleared after 3 attempts — aborting login")
        return False
    code = fetch_verification_code(cfg)
    if not code: shot(driver, "error_no_code.png"); return False
    from selenium.webdriver.common.by import By
    entered = False
    el = find_fill(driver, ["input[type='text']", "input[type='number']", "input[type='tel']", "input[name*='code' i]"], code, "code")
    if el: entered = True
    if not entered:
        vis = [i for i in driver.find_elements(By.CSS_SELECTOR, "input") if i.is_displayed() and i.get_attribute("type") in ("text","number","tel","")]
        if len(vis) >= len(code):
            for i, d in enumerate(code): vis[i].send_keys(d)
            entered = True
    if not entered: shot(driver, "error_code_input.png"); return False
    find_click(driver, ["button[type='submit']"], "Verify")
    time.sleep(5); shot(driver, "step4_logged_in.png")
    log.info(f"Logged in: {driver.current_url}")
    return True

def send_sms(driver, number, message, cfg):
    from selenium.webdriver.common.keys import Keys
    log.info(f"Sending to {number}: {message}")
    driver.get("https://www.textnow.com/messaging")
    time.sleep(4); shot(driver, "step5_messaging.png")
    find_click(driver, get_selectors(cfg, "new_message", [
        "button[aria-label*='new' i]", "button[aria-label*='compose' i]", "a[href*='new']",
        "[class*='compose']", "[class*='newMessage']", "[class*='new-message']",
        "[data-testid*='new']", "button[class*='fab']", "#newMessage"]), "New message")
    time.sleep(2)
    el = find_fill(driver, get_selectors(cfg, "recipient", [
        "input[placeholder*='number' i]", "input[placeholder*='name' i]", "input[placeholder*='to' i]",
        "input[placeholder*='search' i]", "input[aria-label*='to' i]", "input[aria-label*='recipient' i]",
        "input[name*='to' i]", "[class*='recipient'] input", "[class*='searchInput'] input"]), number, "recipient")
    if not el: shot(driver, "error_no_to.png"); return False
    time.sleep(1); el.send_keys(Keys.RETURN); time.sleep(2)
    el = find_fill(driver, get_selectors(cfg, "message_input", [
        "textarea", "input[placeholder*='message' i]", "input[placeholder*='type' i]",
        "[contenteditable='true']", "[class*='messageInput']", "[class*='message-input']",
        "[class*='chatInput']", "[aria-label*='message' i]", "#message-input"]), message, "message")
    if not el: shot(driver, "error_no_msg.png"); return False
    time.sleep(1)
    sent = find_click(driver, get_selectors(cfg, "send_button", [
        "button[aria-label*='send' i]", "[class*='sendButton']", "[class*='send-button']",
        "[data-testid*='send']", "button[type='submit']"]), "Send")
    if not sent:
        log.info("No send button — pressing Enter")
        el.send_keys(Keys.RETURN); sent = True
    time.sleep(3); shot(driver, "step6_sent.png")
    if sent: log.info("SMS sent")
    else: shot(driver, "error_send.png")
    return sent

def validate_config(cfg):
    errors = []
    for f in ("textnow_email", "imap_app_password", "send_to_number", "messages"):
        if f not in cfg or not cfg[f]: errors.append(f"Missing: {f}")
    em = cfg.get("textnow_email", "")
    if em and ("@" not in em or em == "your_email@gmail.com"): errors.append(f"Invalid email: {em}")
    ap = cfg.get("imap_app_password", "")
    if ap and ap.startswith("xxxx"): errors.append("App password is template value")
    num = cfg.get("send_to_number", "")
    if num and not num.startswith("+"): errors.append(f"Number must start with +: {num}")
    msgs = cfg.get("messages", [])
    if isinstance(msgs, list) and len(msgs) == 0: errors.append("No messages defined")
    cron = cfg.get("cron_schedule", "")
    if cron and len(cron.strip().split()) != 5: errors.append(f"Bad cron: {cron}")
    return errors

class RunLock:
    def __init__(self): self.locked = False
    def acquire(self):
        if LOCK_PATH.exists():
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age > 600: LOCK_PATH.unlink()
            else: log.warning(f"Locked ({int(age)}s)"); return False
        with open(LOCK_PATH, "w") as f: f.write(str(os.getpid()))
        self.locked = True; return True
    def release(self):
        if self.locked:
            try: LOCK_PATH.unlink(missing_ok=True)
            except: pass
            self.locked = False

def cleanup_screenshots():
    if not LOG_DIR.exists(): return
    cutoff = time.time() - 7 * 86400
    for f in LOG_DIR.glob("*.png"):
        try:
            if f.stat().st_mtime < cutoff: f.unlink()
        except: pass

def append_history(entry):
    history = []
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH) as f: history = json.load(f)
        except: pass
    entry["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    history.append(entry)
    if len(history) > MAX_HISTORY: history = history[-MAX_HISTORY:]
    with open(HISTORY_PATH, "w") as f: json.dump(history, f, indent=2)

def run_once(dry_run=False):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_screenshots()
    try: cfg = load_config()
    except Exception as e: return {"ok": False, "error": str(e)}
    errs = validate_config(cfg)
    if errs: return {"ok": False, "error": "; ".join(errs)}
    number = cfg["send_to_number"]
    message = render_message(get_next_message(cfg))
    driver = create_driver(cfg)
    try:
        logged_in = False
        if COOKIE_PATH.exists():
            log.info("Trying cookies")
            if load_cookies(driver):
                driver.get("https://www.textnow.com/messaging"); time.sleep(5)
                url = driver.current_url.lower()
                shot(driver, "cookie_check.png")
                if "/login" not in url and "/enter-email" not in url and "/signup" not in url:
                    logged_in = True; log.info(f"Cookie login OK — URL: {driver.current_url}")
                else:
                    log.info(f"Cookies expired or invalid — redirected to: {driver.current_url}")
        if not logged_in: logged_in = full_login(driver, cfg)
        if not logged_in:
            shot(driver, "error_login_failed.png")
            log.error(f"Login failed — URL at failure: {driver.current_url}")
            return {"ok": False, "error": "Login failed"}
        save_cookies(driver)
        if dry_run:
            log.info(f"DRY RUN — would send to {number}: {message}")
            shot(driver, "dry_run.png")
            return {"ok": True, "dry_run": True, "number": number, "message": message}
        sent = send_sms(driver, number, message, cfg)
        if sent: confirm_message_sent(cfg); return {"ok": True, "number": number, "message": message}
        else: return {"ok": False, "error": "Send failed"}
    except Exception as e:
        log.error(f"Error: {e}"); shot(driver, "error.png")
        return {"ok": False, "error": str(e)}
    finally:
        try: driver.quit()
        except: pass

def run_with_retry(dry_run=False):
    lock = RunLock()
    if not lock.acquire(): return {"ok": False, "error": "Locked"}
    try: cfg = load_config()
    except: cfg = {}
    retries = cfg.get("retry_count", 2)
    delay = cfg.get("retry_delay", 30)
    error = "Unknown"
    try:
        for attempt in range(1, retries + 1):
            log.info(f"=== Attempt {attempt}/{retries} ===")
            result = run_once(dry_run=dry_run)
            if isinstance(result, dict) and result.get("ok"):
                detail = f"To: {result.get('number')}\nMessage: {result.get('message')}"
                if not dry_run: append_history({"status": "sent", "number": result.get("number"), "message": result.get("message"), "attempt": attempt})
                send_notifications(cfg, True, detail)
                return result
            error = result.get("error", "Unknown") if isinstance(result, dict) else "Unknown"
            log.warning(f"Attempt {attempt} failed: {error}")
            if attempt < retries: log.info(f"Retrying in {delay}s..."); time.sleep(delay)
        append_history({"status": "failed", "error": error, "attempts": retries})
        send_notifications(cfg, False, f"Error: {error}\nFailed after {retries} attempts.")
        return result
    finally: lock.release()

from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import datetime

# ---------------------------------------------------------------------------
# Minimal cron scheduler (replaces busybox crond)
# ---------------------------------------------------------------------------

def cron_matches(cron_str, dt):
    """Check if a datetime matches a 5-field cron expression."""
    parts = cron_str.strip().split()
    if len(parts) != 5:
        return False

    fields = [
        (parts[0], dt.minute, 0, 59),
        (parts[1], dt.hour, 0, 23),
        (parts[2], dt.day, 1, 31),
        (parts[3], dt.month, 1, 12),
        (parts[4], dt.weekday(), 0, 6),  # 0=Mon in Python, cron uses 0=Sun
    ]

    # Convert Python weekday (0=Mon) to cron weekday (0=Sun)
    py_wday = dt.weekday()
    cron_wday = (py_wday + 1) % 7

    fields[4] = (parts[4], cron_wday, 0, 6)

    for expr, current, lo, hi in fields:
        if not _cron_field_matches(expr, current, lo, hi):
            return False
    return True


def _cron_field_matches(expr, value, lo, hi):
    """Check if a single cron field matches a value."""
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/", 1)
            step = int(step)

        if part == "*":
            if (value - lo) % step == 0:
                return True
        elif "-" in part:
            start, end = part.split("-", 1)
            start, end = int(start), int(end)
            if start <= value <= end and (value - start) % step == 0:
                return True
        else:
            if value == int(part):
                return True
    return False


def cron_scheduler_thread():
    """Background thread that fires run_with_retry on cron schedule."""
    last_fired_minute = None
    log.info("Cron scheduler thread started")

    while True:
        time.sleep(30)  # check every 30 seconds

        try:
            cfg = load_config()
        except Exception:
            continue

        schedule = cfg.get("cron_schedule", "0 9 * * *")
        now = datetime.datetime.now()

        # Unique key for this minute to prevent double-firing
        minute_key = now.strftime("%Y-%m-%d %H:%M")

        if minute_key == last_fired_minute:
            continue

        if cron_matches(schedule, now):
            log.info(f"Cron trigger: {schedule} matched {minute_key}")
            last_fired_minute = minute_key
            Thread(target=run_with_retry, daemon=True).start()


# ---------------------------------------------------------------------------
# Manual login via Chrome Remote Debugging
# ---------------------------------------------------------------------------

_manual_login_active = False

def _tcp_proxy(listen_port, target_host, target_port, stop_event):
    """Simple TCP relay: 0.0.0.0:listen_port → target_host:target_port"""
    import socket, select, threading
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.settimeout(1)
    try:
        server.bind(("0.0.0.0", listen_port))
        server.listen(5)
        log.info(f"TCP proxy: 0.0.0.0:{listen_port} → {target_host}:{target_port}")
    except Exception as e:
        log.error(f"TCP proxy bind failed: {e}")
        return

    def relay(src, dst):
        try:
            while not stop_event.is_set():
                r, _, _ = select.select([src], [], [], 1)
                if r:
                    data = src.recv(65536)
                    if not data: break
                    dst.sendall(data)
        except Exception:
            pass
        finally:
            src.close(); dst.close()

    while not stop_event.is_set():
        try:
            client, addr = server.accept()
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.connect((target_host, target_port))
            threading.Thread(target=relay, args=(client, upstream), daemon=True).start()
            threading.Thread(target=relay, args=(upstream, client), daemon=True).start()
        except socket.timeout:
            continue
        except Exception:
            if not stop_event.is_set(): break
    server.close()

def manual_login():
    """
    Launch Chrome directly (no Selenium/ChromeDriver) with remote debugging.
    Chrome binds to 127.0.0.1:9224 (ignores 0.0.0.0 for security).
    A TCP proxy relays 0.0.0.0:9222 → 127.0.0.1:9224 so external clients
    can connect via chrome://inspect.
    """
    global _manual_login_active
    if _manual_login_active:
        log.warning("Manual login already in progress")
        return {"ok": False, "error": "Already in progress"}

    _manual_login_active = True
    import subprocess, threading

    proc = None
    proxy_stop = threading.Event()
    try:
        cfg = load_config()
        chrome_ver = cfg.get("chrome_version", "146") if cfg else "146"
        user_agent = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_ver}.0.0.0 Safari/537.36"
        )

        # Chrome uses port 9224 internally (localhost only)
        chrome_debug_port = 9224
        # Proxy exposes it on port 9222 (all interfaces)
        proxy_port = 9222

        cmd = [
            CHROMIUM_PATH,
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,800",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-translate",
            "--disable-sync",
            f"--user-agent={user_agent}",
            f"--remote-debugging-port={chrome_debug_port}",
            "--remote-debugging-address=0.0.0.0",
            "about:blank",
        ]

        log.info("=== Manual Login Mode ===")
        log.info("Launching Chrome...")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(4)

        # Verify Chrome debugging port is active
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{chrome_debug_port}/json")
            resp = urllib.request.urlopen(req, timeout=5)
            tabs = json.loads(resp.read())
            log.info(f"Chrome running — {len(tabs)} tab(s) on port {chrome_debug_port}")

            # Log all tabs
            for tab in tabs:
                log.info(f"  Tab: {tab.get('type','')} id={tab.get('id','')[:12]} url={tab.get('url','')}")

            # Close all background/extension tabs, keep one page tab
            page_tab = None
            for tab in tabs:
                if tab.get("type") == "page" and not page_tab:
                    page_tab = tab
                elif tab.get("id"):
                    try:
                        urllib.request.urlopen(
                            f"http://127.0.0.1:{chrome_debug_port}/json/close/{tab['id']}", timeout=3)
                        log.info(f"Closed: {tab.get('url', '?')}")
                    except Exception:
                        pass

            # Navigate the remaining tab to TextNow login via CDP
            if page_tab:
                nav_url = f"http://127.0.0.1:{chrome_debug_port}/json/navigate?url=https://www.textnow.com/login&id={page_tab['id']}"
                # Use the page's websocket to navigate via CDP HTTP endpoint
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{chrome_debug_port}/json/navigate/{page_tab['id']}?url=https://www.textnow.com/login",
                        timeout=5)
                except Exception:
                    pass
                # Fallback: connect Selenium briefly to navigate
                time.sleep(1)
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:{chrome_debug_port}/json")
                    resp = urllib.request.urlopen(req, timeout=5)
                    check_tabs = json.loads(resp.read())
                    current_url = check_tabs[0].get("url", "") if check_tabs else ""
                    if "textnow.com" not in current_url:
                        log.info("CDP navigate didn't work — using Selenium to navigate")
                        from selenium import webdriver as _wb
                        from selenium.webdriver.chrome.options import Options as _Opts
                        from selenium.webdriver.chrome.service import Service as _Svc
                        _o = _Opts()
                        _o.add_experimental_option("debuggerAddress", f"127.0.0.1:{chrome_debug_port}")
                        _dp = PATCHED_CHROMEDRIVER if os.path.isfile(PATCHED_CHROMEDRIVER) else CHROMEDRIVER_PATH
                        _d = _wb.Chrome(service=_Svc(executable_path=_dp), options=_o)
                        _d.get("https://www.textnow.com/login")
                        time.sleep(2)
                        log.info(f"Navigated to: {_d.current_url}")
                        # Don't quit — leave attached for cookie extraction later
                    else:
                        log.info(f"Navigated to: {current_url}")
                except Exception as e:
                    log.warning(f"Navigation fallback error: {e}")
            else:
                log.warning("No page tab found")

        except Exception as e:
            log.error(f"Chrome debugging port not reachable: {e}")
            return {"ok": False, "error": "Chrome failed to start with debugging"}

        # Start TCP proxy: 0.0.0.0:9222 → 127.0.0.1:9224
        proxy_thread = Thread(target=_tcp_proxy,
                              args=(proxy_port, "127.0.0.1", chrome_debug_port, proxy_stop),
                              daemon=True)
        proxy_thread.start()
        time.sleep(1)

        log.info("")
        log.info("Connect from your computer:")
        log.info(f"  1. Verify: open http://homeassistant.local:{proxy_port}/json in a browser")
        log.info("  2. Open Chrome → chrome://inspect")
        log.info(f"  3. Click 'Configure...' → add 'homeassistant.local:{proxy_port}'")
        log.info("  4. Under 'Remote Target', click 'inspect' on the TextNow page")
        log.info("  5. Complete the login/CAPTCHA in the DevTools window")
        log.info("  6. Once you reach the messaging page, cookies save automatically")
        log.info("")
        log.info("Waiting up to 5 minutes...")

        # Poll Chrome DevTools Protocol for URL changes
        timeout = 300
        start = time.time()
        logged_in = False

        while time.time() - start < timeout:
            time.sleep(5)
            elapsed = int(time.time() - start)

            try:
                req = urllib.request.Request(f"http://127.0.0.1:{chrome_debug_port}/json")
                resp = urllib.request.urlopen(req, timeout=5)
                tabs = json.loads(resp.read())
                if tabs:
                    url = tabs[0].get("url", "").lower()
                    if "/messaging" in url:
                        log.info(f"Login detected! URL: {tabs[0]['url']} ({elapsed}s)")
                        logged_in = True
                        break
                    elif elapsed % 30 == 0:
                        log.info(f"  waiting... ({elapsed}s) — URL: {tabs[0].get('url', '?')}")
            except Exception:
                if proc.poll() is not None:
                    log.error("Chrome process died")
                    return {"ok": False, "error": "Chrome crashed"}

        if logged_in:
            time.sleep(3)

            # Extract cookies via Selenium connecting to the running Chrome
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                opts = Options()
                opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{chrome_debug_port}")
                driver_path = PATCHED_CHROMEDRIVER if os.path.isfile(PATCHED_CHROMEDRIVER) else CHROMEDRIVER_PATH
                svc = Service(executable_path=driver_path)
                driver = webdriver.Chrome(service=svc, options=opts)

                cookies = driver.get_cookies()
                with open(COOKIE_PATH, "w") as f:
                    json.dump(cookies, f)
                log.info(f"Cookies saved: {len(cookies)} cookies to {COOKIE_PATH}")
                driver.quit()
            except Exception as e:
                log.error(f"Cookie extraction failed: {e}")
                return {"ok": False, "error": f"Login succeeded but cookie save failed: {e}"}

            return {"ok": True, "message": f"Login successful, {len(cookies)} cookies saved"}
        else:
            log.warning(f"Manual login timed out after {timeout}s")
            return {"ok": False, "error": f"Timed out after {timeout}s"}

    except Exception as e:
        log.error(f"Manual login error: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        proxy_stop.set()
        if proc and proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=5)
            except: proc.kill()
            log.info("Chrome process terminated")
        _manual_login_active = False

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _json(self, code, data):
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        if self.path == "/run": self._json(200, {"status": "started"}); Thread(target=run_with_retry, daemon=True).start()
        elif self.path == "/dry-run": self._json(200, {"status": "started"}); Thread(target=run_with_retry, kwargs={"dry_run": True}, daemon=True).start()
        elif self.path == "/manual-login": self._json(200, {"status": "started", "debug_port": 9222}); Thread(target=manual_login, daemon=True).start()
        elif self.path == "/validate":
            try:
                cfg = load_config(); errs = validate_config(cfg)
                self._json(200, {"valid": not errs, "errors": errs})
            except Exception as e: self._json(200, {"valid": False, "errors": [str(e)]})
        else: self.send_error(404)
    def do_GET(self):
        if self.path == "/health": self._json(200, {"status": "running", "tz": os.environ.get("TZ", "UTC")})
        elif self.path == "/history":
            h = []
            if HISTORY_PATH.exists():
                try:
                    with open(HISTORY_PATH) as f: h = json.load(f)
                except: pass
            self._json(200, h)
        elif self.path == "/state":
            s = load_state()
            try:
                cfg = load_config(); msgs = cfg.get("messages", [])
                idx = s["message_index"] % len(msgs) if msgs else 0
                s["next_message"] = msgs[idx] if msgs else ""; s["total_messages"] = len(msgs)
            except: pass
            s["lock_active"] = LOCK_PATH.exists()
            s["timezone"] = os.environ.get("TZ", "UTC")
            s["local_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._json(200, s)
        else: self.send_error(404)
    def log_message(self, fmt, *args): log.info(f"HTTP: {fmt % args}")

def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

if __name__ == "__main__":
    ensure_dirs()
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--server", action="store_true")
    p.add_argument("--port", type=int, default=7900)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--validate", action="store_true")
    args, _ = p.parse_known_args()
    if args.validate:
        try:
            cfg = load_config(); errs = validate_config(cfg)
            if errs:
                for e in errs: print(f"  - {e}")
                sys.exit(1)
            print("Config OK")
        except Exception as e: print(f"Error: {e}"); sys.exit(1)
    elif args.server:
        # Start cron scheduler in background thread
        cron_thread = Thread(target=cron_scheduler_thread, daemon=True)
        cron_thread.start()
        log.info(f"TZ={os.environ.get('TZ', 'UTC')} — local time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            cfg = load_config()
            log.info(f"Cron schedule: {cfg.get('cron_schedule', '0 9 * * *')}")
        except Exception:
            pass
        srv = HTTPServer(("0.0.0.0", args.port), Handler)
        log.info(f"HTTP server on port {args.port}")
        srv.serve_forever()
    else:
        run_with_retry(dry_run=args.dry_run)
