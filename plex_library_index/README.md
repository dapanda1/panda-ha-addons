# Noah's Home Assistant Add-ons

Personal Home Assistant add-on repository.

## Add-ons

### [Plex Library Index](./plex_library_index)

Scans a Plex Media Server library and serves a searchable index inside Home
Assistant via ingress (sidebar entry, HA-authenticated, mobile-app
compatible, no exposed port).

## Installation

In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
then add:

```
https://github.com/<your-username>/<this-repo>
```

The add-ons listed above will appear in the store under this repository's
name. Click an add-on to install it.

## Local development

If you're working on the add-on locally instead of installing from GitHub:

1. Copy the `plex_library_index/` directory to `/addons/` on your HA host
   (via the Samba add-on or SSH).
2. **Settings → Add-ons → ⋮ → Check for updates**.
3. The add-on appears under **Local add-ons**.
