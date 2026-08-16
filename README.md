# MTG Collection & Deck Dashboard — Data Layer

This is the foundation layer: SQLite schema + migration scripts that convert
your existing CSV exports into a real relational database. The Streamlit
dashboard UI and the print-to-PDF deck report are separate next phases —
this package gets your data into a solid, queryable state first.

## What's here

```
mtg_dashboard/
├── run.py                            # cross-platform launcher — start here
├── run.bat                           # double-click on Windows
├── run.command                       # double-click on Mac
├── run.sh                            # run on Linux
├── schema.sql                        # full DB schema (DDL)
├── requirements.txt
├── data/                              # your source CSVs go here
│   ├── decks.csv
│   ├── maybeboard.csv
│   ├── collection.csv
│   ├── deck_mapping.csv
│   ├── games_played.csv
│   ├── game_changers.csv             # your custom Game Changer categories
│   ├── mana_tags.csv                 # your optimized-mana categories
│   └── deck_themes.csv               # main/sub theme per deck
└── scripts/
    ├── scryfall_lookup.py            # Scryfall API client (lazy fetch + cache)
    ├── migrate.py                    # main migration script — run this
    ├── export_moxfield.py            # deck → Moxfield paste format
    ├── sync_images.py                 # local image cache: download + prune
    └── _test_migrate_offline.py      # dev-only: validates logic with fake
                                       # data, no network needed. Not part
                                       # of your real workflow — ignore unless
                                       # you're modifying the schema/scripts.
```

## Setup

```bash
cd mtg_dashboard
pip install -r requirements.txt
```

## Easiest way to run this: the launcher

You don't need to touch a terminal for day-to-day use. Double-click:

- **Windows:** `run.bat`
- **Mac:** `run.command` (first time, right-click → Open, since it's
  unsigned — macOS will ask you to confirm once)
- **Linux:** `run.sh` from a terminal (`./run.sh`), or just `python3 run.py`

First run creates a virtual environment (`.venv/`) and installs
dependencies automatically — that's a one-time ~30 second wait. Every run
after that opens straight into a menu:

```
1) Run / refresh database migration (rebuilds from CSVs in data/)
2) Launch dashboard
3) Export a deck to Moxfield format
4) Sync image cache (download new + prune unused)
5) Exit
```

If `requirements.txt` ever changes (e.g. once the Streamlit dashboard adds
`streamlit` as a dependency), the launcher detects that and re-installs
automatically — you'll never need to remember to `pip install` again.

Option 2 currently prints a "not built yet" message, since the Streamlit
dashboard itself is the next phase of the project — the launcher is
already wired for it, so it'll just work once `dashboard.py` exists.

I tested every menu path (blocked options before migration, invalid
input, migration → dashboard placeholder, exit) with a mocked
environment; the one thing I couldn't verify here is the real
`pip install` step itself, since this sandbox has no network access —
that'll run for real the first time you double-click the launcher.

### Manual alternative

If you'd rather not use the launcher, everything still works the way it
did before — see "Running the migration" and "Moxfield export" below.

## Running the migration (manual)

```bash
cd scripts
python migrate.py
```

This will:
1. Wipe and rebuild `../mtg_collection.db` from `schema.sql`
2. Load decks, win conditions/strengths/weaknesses, themes (if present)
3. Load your collection — **one Scryfall API call per unique printing**
   (~1,950 calls, exact `set/collector_number` lookups, so this is a real
   printing match, not a fuzzy guess). At ~100ms/call this takes roughly
   3-4 minutes.
4. Load decklists + maybeboards, preferring the exact printing you already
   own (matched from step 3) so Moxfield exports show the correct art.
   Only cards you don't yet own trigger a fresh by-name Scryfall lookup
   (~1,415 of these, based on your current data — same time cost as above).
5. Load the game log
6. Print a full resolution report — **read this**. It'll flag anything
   that didn't match cleanly (mostly typo'd "Replace" targets in your
   maybeboard notes — those are kept as free text, not FK-linked, and
   don't block anything else).

Re-running `migrate.py` is safe — it deletes and rebuilds the `.db` file
from scratch each time rather than trying to diff/update.

## What I already validated

I dry-ran the full migration logic against your real CSVs using a mocked
Scryfall client (no live API calls, synthetic card data) to catch bugs
before handing this off. Confirmed:
- Raktres totals exactly 100 cards, matching your data
- Retired deck linkage works (`Rakdos Showstopper → is_active=0`,
  `successor_deck_id → Raktres`)
- Strategy tag counts for Raktres match your PDF **exactly** (Card Adv 18,
  Pinger 12, Rakdos Target 10, Removal 6, Utility 6, Ramp 5, Interaction 5,
  Protection 4, Big Damage 4)
- Win conditions parse correctly (`"Big Bois: Big Mana Threats..."` →
  label/description split)
- 465 of 1,879 decklist rows already resolve straight from your owned
  collection with zero extra API calls; only 1 maybeboard card
  ("Cantankerous Keepers") couldn't be found on Scryfall at all — worth
  double-checking that name
- `game_changer_tags` (63 rows) and `mana_tags` (89 rows) load cleanly,
  and the drift-detection view correctly flags disagreements between
  your custom list and the (mocked, for this test) Scryfall flag in
  both directions
- `deck_themes.csv` loads correctly — 74 theme assignments across all 22
  tracked decks, and Raktres's main theme resolves to "Group Slug",
  matching your PDF exactly

What I *couldn't* test here: the real Scryfall responses, since this
sandbox has no network access. Read the resolution report closely on your
first real run — synthetic data can't catch things like a real 404 or an
unexpected card-face format.

## Game Changers — two sources, kept in sync

`cards.is_game_changer` is pulled **live from Scryfall's own official
`game_changer` field** on every card fetch — no manual list to maintain,
and it updates automatically if/when WotC revises the Bracket list.

Your `game_changers.csv` (63 cards, categorized as Combo/Mana/Power/
Stax-Unfun/Tutor/Value) loads into a separate `game_changer_tags` table,
giving you the sub-category detail Scryfall's flat boolean doesn't
provide. The two are cross-checked at the end of migration — anything
where they disagree gets printed in the resolution report, e.g.:

- **In your list, Scryfall says no** — your list may be ahead of a recent
  Scryfall data update, or the card may have been reviewed off the list.
- **Scryfall says yes, not in your list** — a card WotC added that your
  categorization hasn't caught up to yet; worth adding a category tag.

Query both together for a deck:

```sql
SELECT card_name, scryfall_flag, custom_tag
FROM deck_game_changers dgc
JOIN decks d ON d.deck_id = dgc.deck_id
WHERE d.name = 'Vilis, Blood ATM';
```

## Optimized mana sources

`mana_tags.csv` loads into a `mana_tags` table (Fast, Dual, Shockland,
Fetch, Ritual, Mana Doubler, Medallion, Moxen, etc.) — same static,
name-keyed pattern as Game Changers, no Scryfall calls needed. Reconstructs
the old "Optimized Mana" PDF footnote dynamically, per deck:

```sql
SELECT tag, cards FROM deck_mana_tag_summary dmts
JOIN decks d ON d.deck_id = dmts.deck_id
WHERE d.name = 'Vilis, Blood ATM';
-- Fast          | Sol Ring
-- Mana Doubler   | Bubbling Muck, Nirkana Revenant, Magus of the Coffers, Cabal Coffers
-- Medallion      | Jet Medallion
-- Ritual         | Dark Ritual
```

## Banlist / legality check

`cards.commander_legal` is populated straight from Scryfall's legalities
data during migration. Quick check for anything illegal in a deck:

```sql
SELECT d.name AS deck, c.name AS card
FROM deck_cards dc
JOIN decks d ON d.deck_id = dc.deck_id
JOIN cards c ON c.scryfall_id = dc.scryfall_id
WHERE c.commander_legal = 0;
```

## Moxfield export

```bash
python export_moxfield.py --list                              # see all deck names
python export_moxfield.py "Raktres, Lord of Discounts"         # print to stdout
python export_moxfield.py "Raktres, Lord of Discounts" -o raktres.txt --maybeboard
```

Output format:
```
1 Combustible Gearhulk (C21) 163
1 Birgi, God of Storytelling // Harnfel, Horn of Bounty (KHM) 129
```

Uses the exact printing you own wherever possible (see step 4 above).

## Win/loss stats

Computed dynamically, never stored — always reflects the live game log:

```sql
SELECT * FROM deck_stats WHERE games_played > 0 ORDER BY win_rate DESC;
```

## Local image cache

`cards.image_uri` stores the remote Scryfall URL, but the dashboard
shouldn't have to re-fetch every image over the network on every load.
`sync_images.py` downloads each referenced printing's image once to
`image_cache/<scryfall_id>.jpg` (one file per exact printing, matching
your owned art) and records the local path in `cards.local_image_path`.

```bash
python sync_images.py              # download missing + prune orphaned
python sync_images.py --no-prune   # download only
python sync_images.py --prune-only # prune only, skip downloading
python sync_images.py --force      # re-download everything, even if cached
```

**Pruning** removes cached files for any printing no longer referenced
by your collection, any decklist, or any maybeboard — e.g. after you cut
a card from a deck and it isn't sitting anywhere else. It clears
`local_image_path` back to NULL for those rows too (not the whole
`cards` row — card metadata is cheap to keep; images are the heavy part),
so if that printing gets referenced again later, it just re-downloads
cleanly rather than pointing at a dangling path.

Run it via the launcher (option 4) any time after a migration that adds
new cards, or periodically to clear out anything orphaned by deck edits.
I validated the full download → link → prune → re-link cycle against the
mock database (including deliberately orphaning 5 cached images and
confirming both the files and their DB paths were cleaned up correctly)
— the one thing I couldn't test here is a real Scryfall image download,
since this sandbox has no network access.

## Custom fields: CSV today, dashboard-editable later

Strategy/Rule 0 tags, Game Changer categories, and mana tags are all
CSV-driven right now (`decks.csv`'s `Custom Tags` column, `game_changers.csv`,
`mana_tags.csv`) — you edit the file, then re-run the migration to pull
the change in. There's no in-tool editing yet since that UI lives in the
not-yet-built Streamlit dashboard.

One thing to know going in: `migrate.py` currently **wipes and rebuilds
the whole database on every run**. That's fine while CSVs are the only
source of truth, but once the dashboard lets you tag cards directly (writing
to SQLite), a from-scratch rebuild would silently erase anything you'd
added there since the CSV wouldn't know about it. Before that UI gets
built, `migrate.py` needs to move from "wipe and rebuild" to "upsert",
so re-running it never clobbers dashboard-made edits. Flagging this now
so it's not a surprise later — not yet implemented.

## Next steps (not built yet)

- Streamlit dashboard (collection view, deck+maybeboard combined view,
  tagging UI, mana curve / land-probability charts)
- HTML/CSS print-to-PDF one-pager, styled after your existing deck
  summary PDFs
- EDHREC comparison — on the backburner per your call
