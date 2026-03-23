# HA Media Request — Custom Add-on

## What It Does

Polls a Gmail inbox via IMAP. When an approved sender emails, each line of the body becomes a media request item. The add-on:

1. Sanitizes the email body (strips HTML, entities, non-printable chars)
2. Strips quoted reply content — only new text is processed
3. Splits into lines, stops at signature markers
4. If any line is exactly `wakeup` (case-insensitive), the entire email is discarded
5. Adds each line to a HA Local To-do list as `Item Title - SenderName`
6. Sends a reply email to the sender confirming receipt
7. Unapproved senders get a "Not Approved" reply
8. Creates a persistent notification in HA
9. Sends a mobile push notification via the Companion App
10. Appends to a permanent log file at `/data/media_request_log.txt`
11. Deletes the email after processing
12. When a task is checked off, emails the original requester that it's now available

---

## Prerequisites

Before installing the add-on, set up these HA components:

### 1. Local To-do List

Settings → Devices & Services → Add Integration → **Local To-do** → name it `Media Requests`

This creates entity `todo.media_requests` and automatically appears in the sidebar under the **To-do** panel.

### 2. Gmail App Password

1. Go to https://myaccount.google.com/apppasswords
2. Generate an app password for "Mail"
3. Copy the 16-character password

### 3. Enable IMAP in Gmail

Gmail → Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP → Save

---

## Configuration

After install, go to the add-on's **Configuration** tab. Fill in:

### imap_server

The IMAP server hostname.

Example: `imap.gmail.com`

### imap_port

The IMAP server port. Gmail uses 993 for SSL.

Example: `993`

### imap_username

The full email address used to log in.

Example: `myaccount@gmail.com`

### imap_password

The Gmail App Password generated in the prerequisites. This is NOT your regular Gmail password.

Example: `abcd efgh ijkl mnop`

### imap_folder

Which mailbox folder to monitor.

Example: `INBOX`

### smtp_server

The SMTP server used to send reply emails.

Example: `smtp.gmail.com`

### smtp_port

The SMTP server port. Gmail uses 587 for STARTTLS.

Example: `587`

### reply_from

The "From" address on reply emails. Leave blank to use `imap_username`.

Example: `myaccount@gmail.com`

### send_reply_emails

Whether to send confirmation replies to approved senders. Unapproved senders always get a "Not Approved" reply regardless of this setting.

Example: `true`

### todo_entity_id

The entity ID of your Local To-do list in HA. Find it under Settings → Devices & Services → Local To-do.

Example: `todo.media_requests`

### mobile_notify_service

The HA notify service name for your mobile device. Find it under Settings → Devices & Services → Mobile App. Uses the format `mobile_app_` followed by the device name in lowercase with underscores.

Example: `mobile_app_pixel_10_pro`

### poll_interval_seconds

How often (in seconds) the add-on checks for new emails.

Example: `60`

### approved_emails

Comma-separated list of email addresses allowed to submit requests. If left blank, all senders are accepted. Senders not on this list receive a "Not Approved" reply and the email is deleted.

Example: `james@gmail.com, sarah@outlook.com, bob@yahoo.com`

### name_mappings

Comma-separated pairs of `email=FriendlyName`. This controls how the sender's name appears in the To-do list and log. If a sender isn't mapped, their email username is used instead.

Example: `james@gmail.com=James, sarah@outlook.com=Sarah, bob@yahoo.com=Bob`

With this mapping, an email from `james@gmail.com` containing "Monkey Shines" becomes: `Monkey Shines - James`

### domain_replacements

Comma-separated pairs of `@domain=prefix` for SMS gateway addresses. When someone texts from their phone to the email address, the sender shows up as something like `5551234567@vzwpix.com`. This setting strips the domain and prepends the prefix, turning it into `+15551234567` so it can be matched in `approved_emails` and `name_mappings`.

Example: `@vzwpix.com=+1, @tmomail.net=+1, @txt.att.net=+1`

With this, a text from `5551234567@vzwpix.com` or `15551234567@vzwpix.com` both normalize to `+15551234567@vzwpix.com`. You can then map it in `name_mappings` as `+15551234567@vzwpix.com=James` and approve it as `+15551234567@vzwpix.com` in `approved_emails`.

### ignored_emails

Comma-separated list of email addresses to silently ignore. The first time an ignored sender's email arrives, you get a persistent notification and a mobile push so you know they hit the inbox. All subsequent emails from that sender are deleted with no notification and no reply.

Example: `spam@example.com, newsletters@store.com, noreply@social.com`

---

## Email Format

Senders email plain text, one item per line:

```
Subject: (anything)

Monkey Shines
The Matrix
Blade Runner 2049
```

Result in the To-do list:
- ☐ Monkey Shines - James
- ☐ The Matrix - James
- ☐ Blade Runner 2049 - James

If the sender replies to an existing email thread, only the new content above the quoted reply is processed.

If any line is exactly `wakeup`, the entire email is silently ignored.

Signature lines (starting with `--`, `Best`, `Regards`, `Sent from`, `Unsubscribe`) and everything after them are stripped automatically.

---

## Completion Notifications

When you check off a task in the To-do list, the add-on detects it on the next poll cycle and emails the person who originally requested it:

```
Monkey Shines is Now Available!
```

The requester mapping is stored in `/data/request_tracker.json` inside the add-on container. Once the notification is sent, the item is removed from the tracker. The email goes to the original sender address (including SMS gateway addresses).

---

## Permanent Log

Located at `/data/media_request_log.txt` inside the add-on container (persists across restarts). Format:

```
2026-03-21 14:30:00  Monkey Shines - James
2026-03-21 14:30:00  The Matrix - James
2026-03-21 15:10:00  Alien - Sarah
```

Access it via the add-on's Log tab or SSH into the container.

---

## File Structure

```
ha_media_request/
├── config.json              # Add-on manifest
├── build.json               # Base image per architecture
├── Dockerfile               # Alpine + python3
├── media_requests.py        # Main application
├── icon.png                 # Add-on detail page icon
├── logo.png                 # Add-on store list icon
└── rootfs/
    └── etc/s6-overlay/s6-rc.d/
        ├── media-requests/
        │   ├── type           # longrun
        │   ├── run            # launches python script
        │   └── dependencies.d/
        │       └── base
        └── user/contents.d/
            └── media-requests
```

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Add-on starts then immediately stops | Check the **Log** tab for errors. Usually IMAP auth failure. |
| No items appearing | Verify `approved_emails` includes the sender. Check that the To-do entity name matches `todo_entity_id`. |
| Reply emails not sending | Verify the App Password works for SMTP. Check logs for SMTP errors. |
| Mobile notifications not arriving | Verify the service name under Settings → Devices & Services → Mobile App. Format: `mobile_app_device_name`. |
| `wakeup` not filtering | The word must be on its own line, by itself. Case-insensitive. |
| Signature lines leaking in | The filter catches common patterns. Add custom patterns by editing `extract_lines()` in the Python script. |
| Completion email not sent | Check that the item text in the todo list exactly matches what was added. The tracker file at `/data/request_tracker.json` shows pending items. |
