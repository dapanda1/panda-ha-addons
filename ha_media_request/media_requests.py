"""
Media Request Tracker Add-on for Home Assistant.

Polls Gmail via IMAP. Each line of an approved sender's email becomes
a media request item. Calls HA REST API to:
  - Add items to a Local To-do list
  - Create persistent notifications
Sends a reply to the sender via SMTP confirming receipt.
Maintains a permanent log file on disk.

If any line is exactly "wakeup" (case-insensitive), the entire email
is silently discarded.
"""

import imaplib
import email
import email.header
import smtplib
import json
import os
import re
import time
import html
import logging
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from datetime import datetime

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("media_requests")

# ── Paths ────────────────────────────────────────────────────────────
OPTIONS_PATH = "/data/options.json"
LOG_FILE = "/data/media_request_log.txt"
HA_TOKEN_PATH = os.environ.get("SUPERVISOR_TOKEN", "")

# ── Config ───────────────────────────────────────────────────────────
def load_config():
    with open(OPTIONS_PATH, "r") as f:
        return json.load(f)


def ha_api(method, endpoint, payload=None):
    """Call the HA Supervisor REST API."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    url = f"http://supervisor/core/api/{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        log.error("HA API %s %s → %s: %s", method, endpoint, e.code, body)
        return e.code, body
    except Exception as e:
        log.error("HA API %s %s → error: %s", method, endpoint, e)
        return 0, str(e)


# ── Sanitization ─────────────────────────────────────────────────────
def sanitize_body(raw):
    """Strip HTML, decode entities, remove non-printable chars, normalize whitespace."""
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", "", raw)
    # Decode HTML entities
    text = html.unescape(text)
    # Remove non-printable except newline/tab
    text = re.sub(r"[^\x20-\x7E\n\t]", "", text)
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def strip_quoted_reply(text):
    """Remove quoted reply content from an email, keeping only new text."""
    lines = text.split("\n")
    new_lines = []
    for line in lines:
        stripped = line.strip()
        # Stop at common reply headers
        if re.match(r"^On .+ wrote:$", stripped):
            break
        if re.match(r"^-{3,}\s*Original Message\s*-{3,}$", stripped, re.IGNORECASE):
            break
        if re.match(r"^From:", stripped) and re.search(r"(Sent|Date):", stripped):
            break
        if re.match(r"^>", stripped):
            break
        if re.match(r"^_{3,}$", stripped):
            break
        new_lines.append(line)
    return "\n".join(new_lines)


def extract_lines(body):
    """Split body into non-empty trimmed lines, strip email signature junk."""
    lines = []
    for raw_line in body.split("\n"):
        line = raw_line.strip()
        # Stop at common signature markers
        if re.match(r"^(--|Best\s|Regards|Sent from|Unsubscribe)", line, re.IGNORECASE):
            break
        # Strip leading non-alphanumeric characters
        line = re.sub(r"^[^a-zA-Z0-9]+", "", line)
        if line:
            lines.append(line)
    return lines


def has_wakeup(lines):
    """Return True if any line is exactly 'wakeup' (case-insensitive)."""
    return any(l.lower() == "wakeup" for l in lines)


# ── Sender resolution ───────────────────────────────────────────────
def parse_sender_email(raw_sender):
    """Extract bare email from 'Display Name <addr>' format."""
    match = re.search(r"<([^>]+)>", raw_sender)
    if match:
        return match.group(1).lower().strip()
    return raw_sender.lower().strip()


def apply_domain_replacements(email_addr, replacements):
    """Normalize phone-based email addresses for consistent matching.
    
    For SMS gateway domains, the local part may arrive with or without
    a leading country code or +. This normalizes so that:
      5551234567@vzwpix.com  → +15551234567@vzwpix.com
      15551234567@vzwpix.com → +15551234567@vzwpix.com
      +15551234567@vzwpix.com → +15551234567@vzwpix.com
    
    The full email address (with domain) is preserved.
    
    replacements is a comma-separated string of pattern=replacement pairs.
    Example: @vzwpix.com=+1, @tmomail.net=+1, @txt.att.net=+1
    """
    if not replacements:
        return email_addr
    for pair in replacements.split(","):
        if "=" not in pair:
            continue
        pattern, replacement = pair.split("=", 1)
        pattern = pattern.strip().lower()
        replacement = replacement.strip()
        if pattern and email_addr.endswith(pattern):
            local_part = email_addr[: email_addr.index(pattern)]
            # Strip leading + from local part for normalization
            local_part = local_part.lstrip("+")
            # Get the digit portion of replacement (e.g. "1" from "+1")
            country_code = replacement.lstrip("+")
            # If local part already starts with the country code, just add +
            if country_code and local_part.startswith(country_code):
                return f"+{local_part}{pattern}"
            else:
                return f"+{country_code}{local_part}{pattern}"
    return email_addr


def resolve_name(email_addr, name_mappings):
    """Resolve email to friendly name, falling back to the local part."""
    if email_addr in name_mappings:
        return name_mappings[email_addr]
    return email_addr.split("@")[0].title()


# ── Email body extraction ───────────────────────────────────────────
def get_email_body(msg):
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback to HTML if no plain text
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def decode_header_value(raw):
    """Decode RFC 2047 encoded header values."""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


# ── Core actions ─────────────────────────────────────────────────────
def add_to_todo(entity_id, item_text):
    """Add a single item to the HA Local To-do list."""
    status, body = ha_api("POST", "services/todo/add_item", {
        "entity_id": entity_id,
        "item": item_text,
    })
    if status in (200, 201):
        log.info("Added to todo: %s", item_text)
    else:
        log.error("Failed to add todo item: %s → %s", item_text, body)


def create_notification(title, message):
    """Create a persistent notification in HA."""
    nid = f"media_req_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    status, body = ha_api("POST", "services/persistent_notification/create", {
        "title": title,
        "message": message,
        "notification_id": nid,
    })
    if status in (200, 201):
        log.info("Notification created: %s", title)
    else:
        log.error("Failed to create notification: %s", body)


def send_mobile_notification(config, title, message):
    """Send a mobile push notification via HA Companion App."""
    device_name = config.get("mobile_notify_service", "")
    if not device_name:
        return
    service_name = device_name if device_name.startswith("notify.") else f"notify.{device_name}"
    # The HA services endpoint uses the service name without the domain prefix
    domain, service = service_name.split(".", 1)
    status, body = ha_api("POST", f"services/{domain}/{service}", {
        "title": title,
        "message": message,
        "data": {
            "channel": "Media Requests",
            "importance": "high",
        },
    })
    if status in (200, 201):
        log.info("Mobile notification sent: %s", title)
    else:
        log.error("Mobile notification failed: %s", body)


def send_reply(config, to_addr, subject, items, approved=True):
    """Send an SMTP reply to the sender. Approved senders get a confirmation, others get rejection."""
    smtp_server = config.get("smtp_server", "smtp.gmail.com")
    smtp_port = config.get("smtp_port", 587)
    username = config["imap_username"]
    password = config["imap_password"]
    from_addr = config.get("reply_from", username) or username

    if approved:
        body_lines = [
            "Your media request has been received.",
            f"{len(items)} item(s) added to the list:",
            "",
        ]
        for item in items:
            body_lines.append(f"  • {item}")
    else:
        body_lines = ["Not Approved"]

    msg = MIMEText("\n".join(body_lines))
    msg["Subject"] = f"Re: {subject}"
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(username, password)
            srv.send_message(msg)
        log.info("Reply sent to %s", to_addr)
    except Exception as e:
        log.error("SMTP reply failed to %s: %s", to_addr, e)


def delete_message(imap, msg_id):
    """Permanently delete a message from Gmail."""
    try:
        # Move to Trash — Gmail permanently deletes from Trash after 30 days,
        # or immediately if auto-expunge is on (default).
        imap.store(msg_id, "+X-GM-LABELS", "\\Trash")
        imap.store(msg_id, "+FLAGS", "\\Deleted")
        log.info("Deleted message %s", msg_id)
    except Exception as e:
        log.error("Failed to delete message %s: %s", msg_id, e)


def append_log(friendly_name, items):
    """Append items to the permanent log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        for item in items:
            f.write(f"{ts}  {item} - {friendly_name}\n")
    log.info("Logged %d item(s) for %s", len(items), friendly_name)


# ── IMAP poll ────────────────────────────────────────────────────────
def poll_inbox(config):
    """Connect to IMAP, fetch UNSEEN messages, process each one."""
    server = config.get("imap_server", "imap.gmail.com")
    port = config.get("imap_port", 993)
    username = config["imap_username"]
    password = config["imap_password"]
    folder = config.get("imap_folder", "INBOX")
    todo_entity = config.get("todo_entity_id", "todo.media_requests")
    send_replies = config.get("send_reply_emails", True)

    # Parse approved_emails: comma-separated string → list
    raw_approved = config.get("approved_emails", "")
    approved_emails = [e.strip().lower() for e in raw_approved.split(",") if e.strip()] if raw_approved else []

    # Parse name_mappings: comma-separated "email=Name" pairs → dict
    raw_names = config.get("name_mappings", "")
    name_mappings = {}
    if raw_names:
        for pair in raw_names.split(","):
            if "=" in pair:
                addr, name = pair.split("=", 1)
                name_mappings[addr.strip().lower()] = name.strip()

    domain_replacements = config.get("domain_replacements", "")

    try:
        imap = imaplib.IMAP4_SSL(server, port)
        imap.login(username, password)
        imap.select(folder)
    except Exception as e:
        log.error("IMAP connection failed: %s", e)
        return

    try:
        status, data = imap.search(None, "UNSEEN")
        if status != "OK":
            log.warning("IMAP search returned: %s", status)
            return

        msg_ids = data[0].split()
        if not msg_ids:
            return

        log.info("Found %d unseen message(s)", len(msg_ids))

        for msg_id in msg_ids:
            try:
                process_message(imap, msg_id, config, todo_entity,
                                name_mappings, approved_emails, send_replies,
                                domain_replacements)
            except Exception as e:
                log.error("Error processing message %s: %s", msg_id, e)
    finally:
        try:
            imap.expunge()
            imap.close()
            imap.logout()
        except Exception:
            pass


def process_message(imap, msg_id, config, todo_entity,
                    name_mappings, approved_emails, send_replies,
                    domain_replacements):
    """Process a single email message."""
    status, msg_data = imap.fetch(msg_id, "(RFC822)")
    if status != "OK":
        log.warning("Failed to fetch message %s", msg_id)
        return

    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)

    raw_sender = decode_header_value(msg.get("From", ""))
    sender_email = parse_sender_email(raw_sender)
    # Apply domain replacements (e.g. 5551234567@vzwpix.com → +15551234567)
    lookup_key = apply_domain_replacements(sender_email, domain_replacements)
    subject = decode_header_value(msg.get("Subject", "No Subject"))

    # Approved sender check — match against both raw email and replaced form
    if approved_emails and sender_email not in approved_emails and lookup_key not in approved_emails:
        log.info("Unapproved sender: %s (%s) — sending rejection", sender_email, lookup_key)
        send_reply(config, sender_email, subject, None, approved=False)
        delete_message(imap, msg_id)
        return

    # Extract and sanitize body
    raw_body = get_email_body(msg)
    clean_body = sanitize_body(raw_body)
    # Strip quoted reply content — only process new text
    new_content = strip_quoted_reply(clean_body)
    lines = extract_lines(new_content)

    if not lines:
        log.info("No content lines in email from %s", sender_email)
        return

    # Wakeup check — discard entire email
    if has_wakeup(lines):
        log.info("Wakeup keyword found — ignoring email from %s", sender_email)
        return

    # Resolve friendly name — try replaced form first, then raw email
    friendly_name = resolve_name(lookup_key, name_mappings)
    if friendly_name == lookup_key.split("@")[0].title() and lookup_key != sender_email:
        friendly_name = resolve_name(sender_email, name_mappings)

    # Format items: "Movie Title - FriendlyName"
    formatted_items = [f"{line} - {friendly_name}" for line in lines]

    # 1. Add each to the HA todo list
    for item in formatted_items:
        add_to_todo(todo_entity, item)

    # 2. Persistent notification
    item_list = "\n".join(f"• {line}" for line in lines)
    create_notification(
        f"New Media Request from {friendly_name}",
        f"{len(lines)} item(s) added:\n{item_list}",
    )

    # 3. Mobile push notification
    send_mobile_notification(
        config,
        f"New Media Request from {friendly_name}",
        f"{len(lines)} item(s) added to the list",
    )

    # 4. Reply to sender
    if send_replies:
        send_reply(config, sender_email, subject, lines)

    # 5. Permanent log
    append_log(friendly_name, lines)

    # 6. Delete the email
    delete_message(imap, msg_id)

    log.info("Processed %d item(s) from %s (%s)", len(lines), friendly_name, sender_email)


# ── Main loop ────────────────────────────────────────────────────────
def main():
    log.info("Media Request Tracker starting")
    config = load_config()
    poll_interval = config.get("poll_interval_seconds", 60)

    log.info("Polling every %d seconds", poll_interval)
    log.info("IMAP server: %s", config.get("imap_server", "imap.gmail.com"))
    log.info("Todo entity: %s", config.get("todo_entity_id", "todo.media_requests"))
    log.info("Approved senders: %s", config.get("approved_emails", ["(any)"]))

    while True:
        try:
            poll_inbox(config)
        except Exception as e:
            log.error("Poll cycle error: %s", e)
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
