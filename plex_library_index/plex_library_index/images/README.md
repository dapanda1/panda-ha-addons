# Optional: add-on artwork

Home Assistant displays two images in the add-on store and detail page if
they exist in the add-on directory:

- `icon.png` — square, recommended **128×128 px**. Shown in the sidebar
  and add-on tile.
- `logo.png` — wider, recommended **250×100 px**. Shown at the top of the
  add-on detail page.

Both are optional. If absent, HA uses generic placeholders. Drop them
directly into `plex_library_index/` next to `config.yaml` to use them.

You can delete this `images/` directory once you've added real artwork
(or never need it).
