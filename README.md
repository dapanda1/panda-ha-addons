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

Wakes a sleeping Plex server (Wake-on-LAN) when it's needed, so the machine
hosting Plex can sleep when idle and power up on demand.

> _See the add-on's own README for current configuration and usage details._

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
├── README.md              ← this file
├── repository.yaml        ← repository metadata (name, url, maintainer)
├── plex_library_index/    ← add-on
│   ├── config.yaml
│   ├── Dockerfile
│   └── …
└── plex_wol_listener/     ← add-on
    ├── config.json
    └── …
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
