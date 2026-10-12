# static/

Files Streamlit serves at `app/static/<path>`, because
`.streamlit/config.toml` sets `server.enableStaticServing = true`. The
folder has to sit next to the entry script (`dashboard.py`).

- `image_cache/` — downloaded Scryfall card art (`<scryfall_id>.jpg`, written
  by `scripts/sync_images.py`) and custom deck covers
  (`deck_covers/<deck_id>.png`, saved from the Deck Editor's Deck Info tab).
  It's gitignored. The database stores these paths relative to this folder
  (`image_cache/<file>`), and `formatting.local_image_url()` turns them into
  URLs.

Card grids, deck tiles and the Decks header link images by URL, so the
browser fetches each image once and caches it. Before UI plan Phase 4, every
rerun put every visible image into the page as base64. The downloadable deck
sheet still embeds its images (`card_view.card_image_src(..., embed=True)`)
because it has to work outside the app.

The cache used to live at the project root (`image_cache/`).
`scripts/move_image_cache.py` moves it here. `run.py` runs that move
before launching the dashboard, and `sync_images.py` runs it before
syncing. If you start Streamlit directly, run that script once yourself.
