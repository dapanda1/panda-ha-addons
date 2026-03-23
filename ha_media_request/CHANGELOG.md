# Changelog

## 2.0

- Duplicate detection — items already on the list are skipped
- Config reload without restart — config changes picked up every poll cycle
- Completion notifications — email sender when their request is checked off
- Ignored sender list — notify once, then silently delete all future emails
- Unapproved senders receive a "Not Approved" reply
- Reply stripping — only new content from replied-to emails is processed
- Leading non-alphanumeric characters stripped from each line
- Domain replacement for SMS gateways (normalizes phone-based email addresses)
- Config descriptions shown in the HA add-on Configuration tab
- Email deleted after processing instead of archived
- Version bump to 2.0

## 1.0

- Initial release
- IMAP polling with configurable interval
- Email body sanitization (HTML strip, entity decode, non-printable removal)
- Line-by-line parsing with signature detection
- Wakeup keyword filter (entire email discarded)
- HA Local To-do list integration
- SMTP reply to sender
- Persistent notifications
- Mobile push notifications
- Permanent log file
- Sender → friendly name mapping
- Approved sender whitelist
