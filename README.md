# Panda's Home Assistant Add-ons

A personal repository of Home Assistant add-ons.

## Add-ons in this repository

### 🎬 [Plex Library Index](./plex_library_index)

Scans a Plex Media Server and serves a fast, searchable library index inside
Home Assistant via ingress — authenticated through HA, works in the mobile
app, no exposed port.

- Full-text search across movies and TV shows (title, genre, director, studio, year, resolution)
- Filters: media type, library, recently added, unwatched, 4K-only, shows with missing episodes
- Resolution / codec / file-size detail, including multiple versions of the same title
- Missing-episode detection (flags gaps within a season)
- Optional full per-episode listing
- Time-of-day scheduling with automatic retry when the Plex server is offline
- Notifications via Home Assistant `notify.*` services, the browser, and an optional built-in Telegram bot
- On-demand rescans from the UI or from an automation

### 🔌 [Plex WOL Listener](./plex_wol_listener)

A Home Assistant add-on that proxies Plex traffic and wakes your Plex server
via Wake-on-LAN when a client connects. Includes Plex token validation,
GeoIP blocking, flood detection, auto-sleep, HA sensors, dashboard toggles,
and notifications.

### 📬 [HA Media Request](./ha_media_request)

Polls a Gmail inbox via IMAP. When an approved sender emails, each line of
the body becomes a media request item.

### 💬 [WebURL SMS Login](./weburl_sms_login)

Automated browser login to TextNow with scheduled SMS sending. Signs in with
Chromium (Selenium) on a virtual display and cycles through up to five
pre-defined messages on a cron schedule.

### 💧 [Utility CustConnect Water](./utility_custconnect_water)

Imports hourly water usage from CustConnect utility portals
(`<utility>.custconnect.com/HomeConnect`, or a utility's own domain) into
Home Assistant long-term statistics for the Energy dashboard. Unofficial;
not affiliated with CustConnect or any utility.

- Hourly history with automatic re-import of recent hours to pick up utility corrections
- Works with any CustConnect portal: enter your login page URL or subdomain
- Detects meter units (gallons, CCF/HCF, cubic feet, cubic meters, and more) and converts to gallons
- Sensors for last hour, last full day, billing period usage and projection, same period last year, data freshness, and update status
- Failure alerts via Home Assistant notifications and an optional mobile `notify.*` service
- No exposed ports; outbound connections only

## Installation

In Home Assistant:

1. **Settings → Add-ons → Add-on Store**
2. Open the **⋮** menu (top right) → **Repositories**
3. Add this repository's URL:
   ```
   https://github.com/dapanda1/panda-ha-addons
   ```
4. Close the dialog. The add-ons above appear in the store under
   **Panda's Home Assistant Add-ons**.
5. Click an add-on, then **Install**.

After installing, open the add-on's **Configuration** tab to set it up, then
**Start** it. Add-ons with a web interface appear in the HA sidebar.

## Repository layout

```
panda-ha-addons/
├── README.md                    ← this file
├── LICENSE                      ← MIT license
├── repository.json              ← repository metadata (name, url, maintainer)
├── plex_library_index/          ← add-on
├── plex_wol_listener/           ← add-on
├── ha_media_request/            ← add-on
├── weburl_sms_login/            ← add-on
└── utility_custconnect_water/   ← add-on
```

Each add-on lives in its own top-level folder. The Supervisor reads each
folder's `config.yaml` (or `config.json`) to register the add-on.

## Local development

To work on an add-on locally instead of installing from this repository:

1. Copy the add-on's folder to `/addons/` on your HA host (via the Samba
   add-on or SSH) — e.g. `/addons/plex_library_index/`.
2. **Settings → Add-ons → ⋮ → Check for updates**.
3. The add-on appears under **Local add-ons**. Install, configure, start.
4. After changing files, use the add-on's **⋮ → Rebuild** to force a fresh
   image build (a plain restart reuses the cached image).

## License

MIT — see [`LICENSE`](./LICENSE).
