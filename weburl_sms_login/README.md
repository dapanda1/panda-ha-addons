# WebURL SMS Login

**Home Assistant OS Add-on**

Automated browser login to TextNow with scheduled SMS sending. Logs into your
TextNow account via Chromium (Selenium) running against an Xvfb virtual
display, sends an SMS to a configured phone number, and cycles through up to
5 pre-defined messages on a cron schedule.

---

## Table of Contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Configuration Settings Reference](#configuration-settings-reference)
5. [Cron Schedule Reference](#cron-schedule-reference)
6. [How It Works](#how-it-works)
7. [HTTP Endpoints](#http-endpoints)
8. [Retry Logic](#retry-logic)
9. [Concurrent Run Lock](#concurrent-run-lock)
10. [Dry Run](#dry-run)
11. [Message Cycling](#message-cycling)
12. [Message Templates](#message-templates)
13. [Cookie-Based Fast Login](#cookie-based-fast-login)
14. [Manual Cookie Export](#manual-cookie-export)
15. [Manual Login via Remote Debugging](#manual-login-via-remote-debugging)
16. [CAPTCHA Handling](#captcha-handling)
17. [CAPTCHA Tuning](#captcha-tuning)
18. [Send History](#send-history)
19. [Timezone](#timezone)
20. [Debug Screenshots](#debug-screenshots)
21. [Files Created by the Add-on](#files-created-by-the-add-on)
22. [Logs](#logs)
23. [Troubleshooting](#troubleshooting)
24. [Security](#security)

---

## Requirements

- Home Assistant OS (HAOS) on Raspberry Pi (aarch64) or x86-64
- A TextNow account with an email-based login
- A Gmail account with 2-Step Verification enabled
- A Gmail App Password (see Installation Step 3)

---

## Installation

### Step 1 — Copy the add-on to your HA instance

Using the **Samba** or **SSH** add-on, copy the entire `weburl_sms_login`
folder to:

```
/config/addons/weburl_sms_login/
```

The folder structure should be:

```
/config/addons/weburl_sms_login/
├── config.yaml
├── build.json
├── Dockerfile
├── run.sh
├── first_run.sh
├── migrate.py
├── weburl_sms_login.py
├── icon.png
├── logo.png
└── README.md
```

### Step 2 — Install in Home Assistant

1. Go to **Settings → Add-ons → Add-on Store**
2. Click the **⋮** menu (top right) → **Check for updates**
3. Scroll to **Local Add-ons** — "WebURL SMS Login" should appear
4. Click it → **Install**
5. Wait for the Docker image to build (several minutes on first install)
6. Click **Start**
7. On first start, Selenium is installed to persistent storage (`/data/pip`)
   and the chromedriver binary is patched for anti-detection

### Step 3 — Generate a Gmail App Password

Google does not allow plain password access to IMAP. You need an App Password:

1. Go to https://myaccount.google.com/apppasswords
2. You must have **2-Step Verification** enabled on your Google account
3. Name it (e.g. "HA WebURL")
4. Click **Generate**
5. Copy the 16-character password (format: `abcd efgh ijkl mnop`)
6. Use this as the `imap_app_password` value in the Configuration tab

### Step 4 — Configure the add-on

Go to **Settings → Add-ons → WebURL SMS Login → Configuration** tab.
Fill in all required fields. See the Configuration Settings Reference below.

### Step 5 — Restart the add-on

After editing the configuration, restart the add-on:
**Settings → Add-ons → WebURL SMS Login → Restart**

### Updating

When you replace files on disk and the version number in `config.yaml` has
changed, use **Update** (not Rebuild) in the add-on UI. HA treats version
changes as upgrades. Use **Rebuild** only when files changed but the version
number stayed the same.

---

## Configuration

All settings are configured via the **Configuration** tab in the add-on UI.
Settings are stored in `/data/options.json` by HA and automatically backed
up to `/config/weburl_sms_login/options_backup.json` on each start.

---

## Configuration Settings Reference

### TextNow Account

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `textnow_email` | email | **Yes** | — | Email address for your TextNow account login. |

### Email / IMAP Settings

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `imap_user` | email | No | Value of `textnow_email` | Email address used to log into the IMAP server. Usually the same as `textnow_email`. |
| `imap_app_password` | password | **Yes** | — | 16-character Gmail App Password. Format: `abcd efgh ijkl mnop`. |
| `imap_server` | string | No | `imap.gmail.com` | IMAP server hostname. Change for non-Gmail providers. |
| `imap_port` | integer | No | `993` | IMAP server port. 993 is standard for IMAP over SSL. |
| `sender_filter` | string | No | `textnow` | Keyword matched against From/Subject of incoming emails to find verification codes. |
| `code_timeout` | integer | No | `120` | Max seconds to wait for the verification code email. |
| `code_poll_interval` | integer | No | `5` | Seconds between IMAP checks while waiting for the code. |

### Browser Settings

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `timeout_ms` | integer | No | `20000` | Max time in milliseconds to wait for page loads and elements. |
| `chrome_version` | string | No | `146` | Chrome major version for the user-agent string. Update this when your desktop Chrome updates to keep them matched. To check your version: Chrome → `chrome://version`. |

### SMS Settings

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `send_to_number` | string | **Yes** | — | Recipient phone number in E.164 format: `+15551234567`. |
| `message_1` | string | **Yes** | — | First message in the rotation. Supports date/time templates. |
| `message_2` | string | No | — | Second message (optional). |
| `message_3` | string | No | — | Third message (optional). |
| `message_4` | string | No | — | Fourth message (optional). |
| `message_5` | string | No | — | Fifth message (optional). |

### Schedule Settings

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `cron_schedule` | string | No | `0 9 * * *` | Standard 5-field cron expression. Runs in your HA system timezone. |

### Retry Settings

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `retry_count` | integer | No | `2` | Total attempts before giving up. Set to `1` to disable retries. |
| `retry_delay` | integer | No | `30` | Seconds between retry attempts. |

### CAPTCHA Timing

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `captcha_hold_base` | integer | No | `10` | Minimum seconds to hold the CAPTCHA button. |
| `captcha_hold_random` | integer | No | `10` | Max additional random seconds added to the base hold. Total hold = base + random(0, this). |
| `captcha_post_wait` | integer | No | `5` | Seconds to wait after releasing the CAPTCHA button before checking if it cleared. |

### Custom Selectors

Override the CSS selectors used to find UI elements. Leave blank to use
built-in defaults. Only change these if TextNow updates their page structure.

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `selector_new_message` | string | No | — | CSS selector for the "New Message" button. |
| `selector_recipient` | string | No | — | CSS selector for the recipient/phone number input. |
| `selector_message_input` | string | No | — | CSS selector for the message text input. |
| `selector_send_button` | string | No | — | CSS selector for the Send button. |

### Notifications

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `notify_title_success` | string | No | `WebURL SMS Login — Sent` | HA notification title on success. |
| `notify_message_success` | string | No | `SMS sent successfully.` | HA notification message on success. |
| `notify_title_failure` | string | No | `WebURL SMS Login — Failed` | HA notification title on failure. |
| `notify_message_failure` | string | No | `SMS sending failed. Check add-on logs and screenshots.` | HA notification message on failure. |
| `notify_mobile_device` | string | No | — | HA mobile app device name for push notifications (e.g. `mobile_app_my_phone`). |

### System

| Setting | Type | Required | Default | Description |
|---------|------|----------|---------|-------------|
| `timezone` | string | No | — | Manual timezone override (e.g. `America/Los_Angeles`). Auto-detected from HA if blank. |

---

## Cron Schedule Reference

The `cron_schedule` field uses standard 5-field cron syntax:

```
┌───────── minute (0–59)
│ ┌─────── hour (0–23)
│ │ ┌───── day of month (1–31)
│ │ │ ┌─── month (1–12)
│ │ │ │ ┌─ day of week (0–6, 0=Sunday)
│ │ │ │ │
* * * * *
```

| Expression | Meaning |
|------------|---------|
| `0 9 * * *` | Every day at 9:00 AM |
| `0 9,18 * * *` | Every day at 9:00 AM and 6:00 PM |
| `30 8 * * 1-5` | Weekdays at 8:30 AM |
| `0 */6 * * *` | Every 6 hours |
| `0 9 * * 0` | Sundays at 9:00 AM |

---

## How It Works

### Startup

1. `run.sh` detects timezone from HA system config (or manual override)
2. Installs Selenium to `/data/pip` on first run (persists across restarts)
3. Patches chromedriver binary to remove `cdc_` detection signatures
4. Runs config migration if schema version changed
5. Backs up config to `/config/weburl_sms_login/options_backup.json`
6. Starts Xvfb virtual display on `:99`
7. Logs the installed Chromium version
8. Starts the Python process (HTTP server + cron scheduler)

### Each Send Attempt

1. Load config from HA options, normalize messages/selectors, sanitize phone number
2. Validate config (required fields, email format, phone format, cron syntax)
3. Pick the next message in rotation and apply date/time templates
4. Launch Chromium (non-headless, running against Xvfb) with anti-detection patches
5. Try cookie-based login (load `cookies.json`, navigate to `/messaging`)
6. If cookies are invalid or missing, run full login:
   a. Navigate to TextNow login page
   b. Click "Continue with Email", enter email, click Continue
   c. Handle PerimeterX CAPTCHA (up to 3 attempts)
   d. Poll Gmail IMAP for verification code
   e. Enter code and verify
7. Save fresh cookies
8. Navigate to messaging, compose and send SMS
9. Advance message rotation index
10. Send HA notification (success or failure)
11. Log to send history

### Non-Headless Mode

Chromium runs in normal (headed) mode against the Xvfb virtual display
rather than using `--headless`. This is important because headless mode has
detectable differences in canvas fingerprinting, font rendering, and WebGL
behavior that PerimeterX flags. Xvfb provides a standard X11 display server
that is undetectable — the browser behaves identically to one running on a
real desktop.

---

## HTTP Endpoints

The add-on runs an HTTP server on port **7900** and uses `host_network: true`
so all ports are directly accessible on the HA host IP.

### POST Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /run` | Trigger a full send immediately. |
| `POST /dry-run` | Run the full login flow but don't send the SMS. |
| `POST /manual-login` | Launch Chrome with remote debugging for manual CAPTCHA completion. See [Manual Login via Remote Debugging](#manual-login-via-remote-debugging). |
| `POST /validate` | Validate the current config and return any errors. |

### GET Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Returns `{"status": "running", "tz": "..."}`. |
| `GET /history` | Returns the last 200 send history entries as JSON. |
| `GET /state` | Returns current message index, next message, lock status, timezone, and local time. |

### Usage

From the SSH add-on or Terminal:

```bash
curl -X POST http://homeassistant.local:7900/run
curl -X POST http://homeassistant.local:7900/dry-run
curl -X POST http://homeassistant.local:7900/manual-login
curl http://homeassistant.local:7900/state
```

Or via HA `rest_command` in `configuration.yaml`:

```yaml
rest_command:
  sms_send:
    url: "http://localhost:7900/run"
    method: POST
  sms_dry_run:
    url: "http://localhost:7900/dry-run"
    method: POST
  sms_manual_login:
    url: "http://localhost:7900/manual-login"
    method: POST
```

Then call from Developer Tools → Services or any automation.

---

## Retry Logic

If a send attempt fails, the add-on retries up to `retry_count` times with
`retry_delay` seconds between attempts. Each attempt goes through the full
flow: cookie check → login if needed → compose → send. A notification is
only sent after all attempts are exhausted (failure) or on the first success.

---

## Concurrent Run Lock

A lock file (`run.lock`) prevents overlapping runs. If a cron trigger fires
while a manual run is active, the second run is skipped. Stale locks older
than 10 minutes are automatically cleared.

---

## Dry Run

A dry run (`POST /dry-run`) goes through the full login flow — cookies,
CAPTCHA, verification code — but stops before sending the SMS. Useful for
testing that the login works without actually sending a message.

---

## Message Cycling

Messages are sent in order: message 1, then message 2, and so on. After the
last message, it wraps back to message 1. The current index is stored in
`/config/weburl_sms_login/state.json` and persists across restarts.

The index only advances on a successful send. Failed attempts do not consume
a message.

---

## Message Templates

Messages support date/time placeholders that are replaced at send time:

| Placeholder | Example | Description |
|-------------|---------|-------------|
| `{date}` | `2026-03-22` | ISO date |
| `{time}` | `9:00 AM` | 12-hour time |
| `{time24}` | `09:00` | 24-hour time |
| `{day}` | `Saturday` | Full weekday name |
| `{day_short}` | `Sat` | Abbreviated weekday |
| `{month}` | `March` | Full month name |
| `{month_short}` | `Mar` | Abbreviated month |
| `{month_num}` | `03` | Month number (zero-padded) |
| `{year}` | `2026` | Four-digit year |
| `{day_num}` | `22` | Day of month |
| `{hour}` | `9` | 12-hour hour |
| `{hour24}` | `09` | 24-hour hour (zero-padded) |
| `{minute}` | `00` | Minute (zero-padded) |
| `{weekday_num}` | `5` | Day of week (0=Monday, 6=Sunday) |

### Example

```
"Good {day} morning! It's {time} on {month} {day_num}."
→ "Good Saturday morning! It's 9:00 AM on March 22."
```

Plain text without placeholders is sent as-is.

---

## Cookie-Based Fast Login

After a successful login, the add-on saves browser cookies to
`/config/weburl_sms_login/cookies.json`. On subsequent runs, it loads these
cookies and navigates directly to the messaging page.

If cookies are valid, the entire login flow (CAPTCHA, email, verification
code) is skipped. If cookies are expired (TextNow redirects to login/signup/
enter-email), the add-on falls back to the full login flow automatically.

Cookies are refreshed and re-saved after every successful run.

---

## Manual Cookie Export

If the automated CAPTCHA is consistently rejected, you can bypass it by
exporting cookies from your own browser session. Note: this approach may
not work if TextNow binds sessions to the browser's TLS fingerprint, which
differs between desktop Chrome and Alpine's Chromium. The recommended
alternative is [Manual Login via Remote Debugging](#manual-login-via-remote-debugging).

### Steps

1. On your computer, open Chrome and log into https://www.textnow.com
2. Before logging in, set your browser's user-agent to match the add-on's.
   In Chrome DevTools (F12) → Network conditions tab → User agent →
   uncheck "Use browser default" → paste:
   ```
   Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36
   ```
   (Replace `146` with the value of `chrome_version` in your config.)
3. Log into TextNow with the spoofed user-agent active
4. Install the "Cookie-Editor" browser extension
5. Navigate to https://www.textnow.com/messaging (confirm you're logged in)
6. Click the Cookie-Editor icon → **Export** → **Export as JSON**
7. Save the file as `cookies.json`
8. Copy it to your HA instance via Samba:
   ```
   \\homeassistant\config\weburl_sms_login\cookies.json
   ```
9. Trigger a dry run to verify: `curl -X POST http://homeassistant.local:7900/dry-run`
10. Check the log — it should show `Cookie login OK`

---

## Manual Login via Remote Debugging

When the automated CAPTCHA fails and cookie export doesn't work (due to TLS
fingerprint mismatch), you can manually complete the login through the
add-on's own browser using Chrome's Remote Debugging Protocol.

This is the most reliable method because you're interacting with the actual
Chromium instance on the Pi. Cookies saved from this session will work for
future automated runs since the browser environment matches exactly.

### How It Works

1. The add-on launches Chromium (non-headless, on Xvfb) with remote debugging
   enabled on an internal port (9224, localhost only)
2. A TCP proxy relays external connections from port 9222 to the internal
   debugging port (Chromium forces debugging to localhost)
3. You connect from your desktop Chrome via `chrome://inspect` and interact
   with the browser through DevTools
4. The add-on monitors the page URL — when it detects `/messaging`, it saves
   cookies and closes the session

### Steps

1. Trigger manual login:
   ```bash
   curl -X POST http://homeassistant.local:7900/manual-login
   ```
   Or use a dashboard button / `rest_command.sms_manual_login`.

2. On your computer, open Chrome and go to `chrome://inspect`

3. Click **Configure...** → add your HA IP with port 9222:
   ```
   192.168.68.69:9222
   ```
   **Important:** Use the IP address, not `homeassistant.local`. Chrome's
   DevTools rejects non-IP hostnames.

4. A **Remote Target** should appear showing the TextNow login page.
   Click **inspect**.

5. A DevTools window opens showing the browser on the Pi. Complete the
   login and CAPTCHA manually.

6. Once you reach the messaging page, the add-on detects it, saves cookies,
   and closes the browser. The log will show:
   ```
   Login detected! URL: https://www.textnow.com/messaging
   Cookies saved: N cookies to /config/weburl_sms_login/cookies.json
   ```

7. Future runs will use these cookies and skip the login entirely.

### Timeout

The manual login session stays open for 5 minutes. If you don't complete
the login in time, the browser is closed automatically.

### Troubleshooting Remote Debugging

- **No Remote Target appears:** Check `http://192.168.68.69:9222/json` in
  your browser — it should return JSON. If it doesn't load, the TCP proxy
  may not have started. Check the add-on log for
  `TCP proxy: 0.0.0.0:9222 → 127.0.0.1:9224`.

- **"Host header is specified and is not an IP address or localhost":** Use
  the IP address (`192.168.68.69:9222`) instead of `homeassistant.local:9222`.

- **Multiple entries in Remote Target:** The Google sign-in button on TextNow's
  login page creates an iframe that shows as a separate entry. Click
  **inspect** on the entry showing `https://www.textnow.com/login`.

---

## CAPTCHA Handling

TextNow uses PerimeterX's "Press and Hold" CAPTCHA. The CAPTCHA button lives
inside a closed shadow DOM that Selenium cannot access directly:

```
div#px-captcha → closed shadow root → iframe → div[role=button]
```

The add-on handles this by:

1. Finding the `#px-captcha` container (visible in the regular DOM)
2. Moving the mouse with human-like trajectory toward the button
3. Clicking and holding at the button's coordinates with randomized offset
4. Holding for a configurable duration (default 10–20 seconds)
5. Releasing and waiting for the page to react
6. Checking if `#px-captcha` disappeared, was hidden, or collapsed
7. Retrying up to 3 times if "Please try again" appears

### Anti-Detection Measures

The add-on patches multiple browser fingerprint signals:

- **Non-headless mode** — Chromium runs against Xvfb instead of headless
  mode, producing identical rendering to a real desktop browser
- **Chromedriver binary patching** — `cdc_` variable signatures replaced
  with random strings (primary PerimeterX detection vector)
- **Fonts** — `font-noto`, `font-noto-emoji`, and `fontconfig` installed
  to normalize canvas fingerprinting
- **`navigator.webdriver`** — removed
- **Plugins and mimeTypes** — faked to match real Chrome (names, filenames,
  descriptions)
- **WebGL renderer** — spoofed to Intel Iris OpenGL Engine
- **`window.chrome`** — added with runtime, loadTimes, and csi objects
- **Permissions API** — patched
- **Screen properties** — outerWidth/Height, colorDepth, pixelDepth
- **Platform, languages, hardwareConcurrency, deviceMemory** — set to
  realistic values
- **Network connection** — faked as 4G
- **User-agent** — configurable via `chrome_version`, matches real Chrome
  on Windows
- **`--disable-blink-features=AutomationControlled`** — removes automation
  flag from Chrome
- **`--no-first-run`**, **`--disable-extensions`**, **`--disable-sync`** —
  suppresses Chrome intro pages and built-in extensions
- **Mouse movement** — human-like trajectory with random pauses before
  clicking

If the CAPTCHA still fails consistently, use the
[Manual Login via Remote Debugging](#manual-login-via-remote-debugging)
method to complete it manually. Cookies from that session will be used for
future automated runs.

---

## CAPTCHA Tuning

Three configuration fields control CAPTCHA timing. Adjust these in the
**Configuration** tab without needing code changes:

| Setting | Default | Description |
|---------|---------|-------------|
| `captcha_hold_base` | `10` | Minimum seconds to hold. |
| `captcha_hold_random` | `10` | Random 0–N seconds added to base. |
| `captcha_post_wait` | `5` | Seconds to wait after release before checking. |

The actual hold duration for each attempt is:
`captcha_hold_base + random(0, captcha_hold_random)`

With defaults, each attempt holds between 10 and 20 seconds.

---

## Send History

Every send attempt is logged to `/config/weburl_sms_login/history.json`:

- `timestamp` — when the attempt occurred
- `status` — `"sent"` or `"failed"`
- `number` — recipient (on success)
- `message` — the message text (on success)
- `error` — error description (on failure)
- `attempt` / `attempts` — which retry succeeded or total attempts on failure

Capped at 200 entries. View via `GET http://homeassistant.local:7900/history`.

---

## Timezone

The add-on detects your HA system timezone on startup
(Settings → System → General → Time Zone). The cron schedule and all log
timestamps use this timezone.

If auto-detection fails, set the `timezone` field manually in the
Configuration tab (e.g. `America/Los_Angeles`).

The `/state` endpoint shows the active timezone and local time for
verification.

---

## Debug Screenshots

Screenshots are saved to `/config/weburl_sms_login/screenshots/` at each
step. CAPTCHA screenshots include the attempt number so they don't overwrite
each other across retries.

### Login Flow

| File | When |
|------|------|
| `step1_login.png` | After loading the TextNow login page |
| `step2_email.png` | After entering email and clicking Continue |
| `step3_captcha.png` | After CAPTCHA handling completes |
| `step4_logged_in.png` | After successful login |
| `cookie_check.png` | After cookie login attempt |

### CAPTCHA (per attempt)

| File | When |
|------|------|
| `captcha_a1_pre.png` | Before CAPTCHA check (attempt 1) |
| `captcha_a1_found.png` | After `#px-captcha` detected |
| `captcha_a1_holding.png` | During click-and-hold |
| `captcha_a1_after_release.png` | Immediately after mouse release |
| `captcha_a1_after.png` | After post-wait check |
| `captcha_retry_1.png` | Between retry attempts |

Attempts 2 and 3 use `captcha_a2_*` and `captcha_a3_*`.

### SMS Flow

| File | When |
|------|------|
| `step5_messaging.png` | After loading messaging page |
| `step6_sent.png` | After clicking send |
| `dry_run.png` | Dry run completed (no SMS sent) |

### Error Screenshots

| File | When |
|------|------|
| `error_login_failed.png` | Login failed after all attempts |
| `error_no_email_btn.png` | "Continue with Email" button not found |
| `error_no_email.png` | Email input field not found |
| `error_no_code.png` | Verification code not received |
| `error_code_input.png` | Code input field not found |
| `error_no_to.png` | Recipient input not found |
| `error_no_msg.png` | Message input not found |
| `error_send.png` | Send button not found |
| `error.png` | Generic error (uncaught exception) |

### Manual Login

| File | When |
|------|------|
| `manual_login_start.png` | Manual login session started |
| `manual_login_success.png` | Manual login completed |
| `manual_login_timeout.png` | Manual login timed out |

Screenshots older than 7 days are automatically cleaned up. Access via
Samba at `\\homeassistant\config\weburl_sms_login\screenshots\`.

---

## Files Created by the Add-on

All persistent files in `/config/weburl_sms_login/`:

| File | Purpose |
|------|---------|
| `options_backup.json` | Config backup (survives uninstalls) |
| `state.json` | Message rotation index, send count, last sent time |
| `cookies.json` | Browser session cookies |
| `history.json` | Last 200 send history entries |
| `run.lock` | Concurrency lock (auto-deleted after run) |
| `screenshots/` | Debug and error screenshots (auto-cleaned after 7 days) |

Persistent files in `/data/` (inside the container):

| File | Purpose |
|------|---------|
| `/data/pip/` | Selenium installation (persists across restarts) |
| `/data/chromedriver_patched` | Patched chromedriver binary (`cdc_` signatures replaced with random strings) |
| `/data/.chromedriver_hash` | Hash of source chromedriver (triggers re-patch on update) |
| `/data/.schema_version` | Config schema version (for migrations) |

---

## Networking

The add-on uses `host_network: true`, meaning the container shares the
host's network stack directly. Ports 7900 (HTTP server) and 9222 (Chrome
remote debugging proxy) are accessible on the HA host IP without Docker
port mapping.

---

## Logs

View logs at: **Settings → Add-ons → WebURL SMS Login → Log** tab.

Example startup:

```
2026-04-14 09:00:00 INFO  Timezone set to: America/Los_Angeles
2026-04-14 09:00:01 INFO  Chromium: Chromium 131.0.6778.139
2026-04-14 09:00:01 INFO  Starting WebURL SMS Login...
2026-04-14 09:00:01  INFO  TZ=America/Los_Angeles — local time: 2026-04-14 09:00:01
2026-04-14 09:00:01  INFO  Cron schedule: 0 9 * * *
2026-04-14 09:00:01  INFO  HTTP server on port 7900
```

Example successful run:

```
2026-04-14 09:00:01  INFO  Cron trigger: 0 9 * * * matched 2026-04-14 09:00
2026-04-14 09:00:01  INFO  === Attempt 1/2 ===
2026-04-14 09:00:01  INFO  Message 2/5: Hope your Saturday is going well.
2026-04-14 09:00:03  INFO  Trying cookies
2026-04-14 09:00:04  INFO  Loading 10 cookies from cookies.json
2026-04-14 09:00:04  INFO  Loaded 10/10 cookies
2026-04-14 09:00:09  INFO  Cookie login OK — URL: https://www.textnow.com/messaging
2026-04-14 09:00:14  INFO  SMS sent
2026-04-14 09:00:14  INFO  HA notification sent: WebURL SMS Login — Sent
```

---

## Troubleshooting

**Add-on won't start**
Check the Log tab. Common causes: missing required config fields, syntax
errors in manual config edits.

**"Cookies expired or invalid" every run**
TextNow sessions expire (~24 hours). This is normal — the add-on falls back
to full login. If you exported cookies manually, TLS fingerprint mismatch
between your browser and Alpine's Chromium may prevent them from working.
Use [Manual Login via Remote Debugging](#manual-login-via-remote-debugging)
instead.

**"No verification code received"**
Check `imap_app_password` is a Gmail App Password (not your regular
password). Check Gmail for the TextNow email — it may be in Spam. Increase
`code_timeout` if email delivery is slow.

**CAPTCHA always fails ("Please try again")**
PerimeterX is fingerprinting the browser environment. Try adjusting
`captcha_hold_base` and `captcha_hold_random` in the Configuration tab.
If it consistently fails, use
[Manual Login via Remote Debugging](#manual-login-via-remote-debugging) to
complete the CAPTCHA manually and save cookies from that session.

**Wrong timezone / cron fires at wrong time**
Check `GET http://homeassistant.local:7900/state` — the `timezone` and
`local_time` fields show what the add-on is using. Set `timezone` manually
in the Configuration tab if auto-detection fails.

**Selector errors ("Could not find...")**
TextNow may have updated their UI. Check screenshots in
`/config/weburl_sms_login/screenshots/` to see the current page layout.
Use custom `selector_*` fields if needed.

**"Connection reset by peer" on HTTP endpoints**
The Python process may have crashed. Check the add-on log. Restart the
add-on.

**Version changed, use Update instead Rebuild**
When you change the version number in `config.yaml`, use **Update** (not
Rebuild). HA treats version changes as upgrades. Rebuild is only for
re-building the same version after changing files.

---

## Security

- **Credentials** are stored in HA's `/data/options.json` (password fields
  are masked in the UI). A backup copy exists at
  `/config/weburl_sms_login/options_backup.json` — restrict access to your
  HA config share.
- **Port 7900** is exposed for the HTTP trigger server. It accepts POST
  requests to trigger sends. Restrict access to your local network.
- **Port 9222** is exposed during manual login sessions only (active for
  up to 5 minutes). It provides remote debugging access to the Chrome
  instance.
- **`host_network: true`** — the container shares the host's network stack.
  All listening ports are directly accessible on the host IP.
- **No external data sharing**. The add-on only connects to TextNow
  (login/SMS) and Gmail (verification codes). No analytics or telemetry.
- **Chromedriver patching** modifies the binary locally. No external
  downloads after initial Selenium install.
