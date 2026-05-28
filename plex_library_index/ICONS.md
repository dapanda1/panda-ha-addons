# Add-on artwork (optional)

Home Assistant displays two images on the add-on store page and detail
view if they exist directly in this directory (alongside `config.yaml`):

- **`icon.png`** — square, recommended **128×128 px**. Shown in the
  sidebar and add-on tile.
- **`logo.png`** — wider, recommended **250×100 px**. Shown at the top
  of the add-on detail page.

Both are optional. If absent, Home Assistant uses generic placeholders.

To add artwork:
1. Create or download the images.
2. Drop them in this directory next to `config.yaml`.
3. Commit + push. HA picks them up on next refresh.

You can delete this `ICONS.md` once you've added real artwork (or never
need it).
