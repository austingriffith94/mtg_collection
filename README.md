# MTG Collection & Deck Dashboard

The foundation layer (SQLite schema + migration scripts converting your CSV
exports into a real relational database) is complete and validated. The
Streamlit dashboard is built out across five pages — Collection, Decks &
Maybeboard, Land & Color Probability, Editor, and Commander Game Tracking
(see "Running the dashboard" below). The print-to-PDF deck report is the
remaining next phase.

## What's here

```
mtg_dashboard/
├── run.py                            # cross-platform launcher — start here
├── run.bat                           # double-click on Windows
├── run.command                       # double-click on Mac
├── run.sh                            # run on Linux
├── schema.sql                        # full DB schema (DDL)
├── requirements.txt
├── dashboard.py                      # Streamlit entry point (home page)
├── pages/                             # Streamlit auto-discovers these as nav tabs
│   ├── 1_Collection.py
│   ├── 2_Decks.py
│   ├── 3_Land_Probability.py
│   ├── 4_Editor.py
│   └── 5_Commander_Game_Tracking.py
├── dashboard_lib/                     # shared library code behind the pages
│   ├── formatting.py                 # card-type/color/URL/filename helpers (no Streamlit dep)
│   ├── queries.py                    # read-only sqlite3+pandas queries (no Streamlit dep)
│   ├── probability.py                # hypergeometric draw math (no Streamlit dep)
│   ├── writes.py                     # card tags/deck/decklist/maybeboard/collection/game write layer (no Streamlit dep)
│   ├── card_resolver.py              # find-or-fetch a card for "add a new card" flows (no Streamlit dep)
│   ├── moxfield_export.py            # Moxfield format — shared by the CLI script and the dashboard (no Streamlit dep)
│   ├── refresh.py                    # refresh Reserved List/Game Changer/legality/price by ID, then prune the collection (no Streamlit dep)
│   ├── game_form.py                  # Commander Game Tracking form resolution/validation (no Streamlit dep, Prompt Pass 7)
│   ├── db.py                         # cached connection + "no DB yet" guard
│   ├── loaders.py                    # st.cache_data-wrapped versions of queries.py
│   └── card_view.py                  # shared table/grid browser + filter UI
├── data/                              # your source CSVs go here
│   ├── decks.csv
│   ├── maybeboard.csv
│   ├── collection.csv
│   ├── deck_mapping.csv
│   ├── games_played.csv
│   ├── game_changers.csv             # your custom Game Changer categories
│   ├── mana_tags.csv                 # your optimized-mana categories
│   └── deck_themes.csv               # main/sub theme per deck
├── moxfield_exports/                  # created on first use — .txt files saved from the dashboard
├── image_cache/                       # created on first use — one .jpg per printing (sync_images.py),
│                                       # plus deck_covers/<deck_id>.png for custom deck thumbnails (Editor)
└── scripts/
    ├── scryfall_lookup.py            # Scryfall API client (lazy fetch + cache) — also
    │                                  # reused directly by dashboard_lib/card_resolver.py
    ├── migrate.py                    # main migration script — one-way, CSV -> DB, run once
    ├── export_moxfield.py            # deck → Moxfield paste format (CLI; dashboard has this too)
    ├── export_moxfield_collection.py  # collection → Moxfield CSV import format (CLI; dashboard has this too)
    ├── refresh_card_data.py          # refresh Scryfall fields for existing cards (CLI; dashboard has this too)
    ├── sync_images.py                 # local image cache: download + prune
    └── _test_migrate_offline.py      # dev-only: validates logic with fake
                                       # data, no network needed. Not part
                                       # of your real workflow — ignore unless
                                       # you're modifying the schema/scripts.
```

`dashboard_lib` is split deliberately: `formatting.py`, `queries.py`,
`probability.py`, `writes.py`, `card_resolver.py`, `moxfield_export.py`,
and `refresh.py` have no Streamlit import at all, so they can be (and
were) unit-tested directly against a plain sqlite3 connection without a
running dashboard.
`db.py`, `loaders.py`, and `card_view.py` hold the Streamlit-specific
caching and widgets.

## Setup

The migration/export/sync scripts need exactly two third-party packages;
the dashboard itself additionally needs `streamlit`. Everything else used
is Python standard library. Install into whatever environment you're
managing yourself (conda, venv, VS Code's env picker, etc.):

```bash
pip install pandas requests streamlit
# or
conda install pandas requests streamlit
```

`requirements.txt` lists the same three, if you'd rather point a tool at
a file (`pip install -r requirements.txt` / `conda install --file requirements.txt`).

**Which script/page needs what**, if you want to scope an environment more
tightly:

| Script/page | pandas | requests | streamlit |
|---|:---:|:---:|:---:|
| `migrate.py` | ✅ | ✅ (via `scryfall_lookup.py`) | — |
| `sync_images.py` | — | ✅ | — |
| `export_moxfield.py` | — | — | — |
| `export_moxfield_collection.py` | — | — | — |
| `refresh_card_data.py` | — | ✅ (via `scryfall_lookup.py`) | — |
| `dashboard.py` + `pages/*.py` | ✅ | optional* | ✅ |

`pandas` is used by migration and the dashboard, but not image sync or
either Moxfield export. `streamlit` is dashboard-only. Both Moxfield
exports are pure stdlib — no install needed at all for those two.

\* `requests` is only needed by the Editor page's "add a card" forms
(Mainboard/Maybeboard/Collection tabs) and the "Card Data" tab's refresh
button, and only when live network access is actually required (a new
card not already in your database, or the refresh itself) — it reuses
`scripts/scryfall_lookup.py` for those. Everything else in the dashboard,
including editing cards you already have, works without `requests`
installed.

## Easiest way to run this: the launcher

The launcher (`run.py`) does **not** install anything or manage
environments for you — by design, so you stay in control of your own
conda/venv setup. It runs using whatever Python interpreter is currently
active when you launch it (prints which one at the top of the menu, so
you can confirm you're in the right environment), and checks each
action's specific dependencies right before running it — e.g. picking
"Export to Moxfield" never complains about pandas, since that action
doesn't need it. If something's missing for the action you picked,
it prints the exact `pip`/`conda` install command and stops, rather than
installing anything on its own.

It still checks you're on Python 3.8+ and gives a clear message (not a
raw crash) if not — that's a version check, not a package install, so
it stays regardless of how you manage dependencies.

You don't need to touch a terminal for day-to-day use — just make sure
your environment is activated first. Double-click:

- **Windows:** `run.bat` (activate your conda/venv env in the same
  terminal first if you're not using a global install)
- **Mac:** `run.command` (first time, right-click → Open, since it's
  unsigned — macOS will ask you to confirm once)
- **Linux:** `run.sh` from a terminal (`./run.sh`), or just `python3 run.py`

Opens straight into a menu:

```
1) Launch dashboard
2) Export a deck to Moxfield format
3) Export collection to Moxfield CSV
4) Sync image cache (download new + prune unused)
5) Refresh Scryfall card data (Reserved List/Game Changer/legality/price)
6) Exit

Rarely needed once you're up and running — type the word, not a number:
  migrate) Rebuild the database from CSVs in data/ (⚠ discards any dashboard-made edits since your last migration)
```

The one-time/rare CSV migration is deliberately **not** a numbered option —
`migrate.py` wipes and rebuilds `mtg_collection.db` from scratch every
run (see "Migration is one-way" below), so it's tucked behind typing the
word `migrate` instead of a digit, and asks you to type `REBUILD` to
confirm before it actually touches anything. The first time you run
`run.py` with no `mtg_collection.db` yet, it'll tell you to type
`migrate` to get started.

If `requirements.txt` ever changes again, you'll need to install the new
package yourself into your active environment — the launcher won't do it
for you, in keeping with staying out of your environment management.

Option 1 launches the real Streamlit dashboard (`dashboard.py`, plus
everything under `pages/`) — make sure `streamlit` is installed (see
"Setup" above), then run the migration first if you haven't already.

The dashboard currently covers:
- **Home** — a landing page of image-backed deck tiles (cover image if
  you've set one, else the commander's own card art); clicking a tile
  jumps straight to that deck on the Decks page below.
- **Collection** — table or image-grid view (48 cards per page by default)
  of your full physical collection, with layered filters (color, type,
  rarity, set, location, foil, basic land, Game Changer, Reserved List,
  Showcase, Borderless, price, mana value), a deck-status toggle (In a
  deck / Not in a deck / Not in a deck OR another location), and
  multi-level "group by" breakouts. Every card links out to its Scryfall
  page from both views. A "📋 Export to Moxfield (Collection CSV)" panel
  generates a Moxfield-compatible collection import CSV — Full Collection
  or Owned NOT in a deck — downloadable or saved to `moxfield_exports/`
  (see "Moxfield export" below).
- **Decks** (still covers the maybeboard, via the Board toggle near the
  bottom) — pick any deck (or arrive pre-selected from a home-page tile)
  for a "Turn 0" summary: commander/partner, colors, bracket, interaction,
  combos, tutors, win/loss record, deck value, and how many of its cards
  are physically sleeved in it; an optional custom cover-image thumbnail;
  win conditions/strengths/weaknesses; directly under that, optimized-mana
  summary, a Game Changers table showing your own category tag (drift vs.
  Scryfall highlighted), and a Reserved List table showing each card's
  current price; then themes, mana curve (grouped/colored W, U, B, R, G,
  Multi-color, Colorless), type breakdown, strategy tag breakdown; then
  Top 10 Most Expensive (unit price plus what you actually paid for your
  copy, if logged) list (the Top 10 Saltiest card list that used to sit
  alongside it, backed by hand-maintained EDHREC salt data, was removed —
  see "EDHREC Salt Score" below); and an in-panel Moxfield export
  (copy-to-clipboard code box, or save a `.txt` file, mainboard-only or
  with maybeboard). Below that, the same table/grid browser as Collection,
  scoped to Mainboard, Maybeboard, or both at once.
- **Land & Color Probability** — per-deck hypergeometric draw math: the
  probability of lands showing up in your opening 7, an estimate of
  hitting your land drop each of the first 5 turns (plus the
  probability-weighted EXPECTED number of lands seen by each of those
  turns), the probability of having a mana source for each color in the
  commander's color identity at your opening hand and each of the first 5
  turns (own text-based parser, so "any color" lands like Command Tower
  count correctly even though their color identity is blank; optionally
  also counts nonland mana rocks/dorks that have a verified mana ability
  of their own — a same-colored nonland card with no mana ability never
  counts), a weighted mana-source-availability graph (expected source
  counts by category, turn-by-turn), a Commander Cast Probability engine
  (turns 1–10, compounding land-drop and color-source odds for your
  commander), and a per-card Cast Probability tool for any chosen
  mainboard card (opening hand through turn 8) that factors in that
  card's own mana value and colored pips — so two singletons with
  different costs show different curves, not an identical draw-odds line.
  Library size and land count auto-detect from the decklist
  (commander/partner excluded, since they live in the command zone, not
  the shuffled deck) but are editable if you want to test a hypothetical.
- **Editor** — add, rename, and remove things directly, no CSV editing
  required:
  - **Deck Info** — rename a deck (with an option to also update any
    `collection.location` rows that matched the old name, so "sleeved in
    this deck" tracking stays correct), edit commander/partner/colors/
    bracket/interaction/description/combos/tutors, upload a custom PNG as
    the deck's cover image, edit Win Conditions/Strengths/Weaknesses (up
    to 3 ranked entries each), create a brand new deck from scratch
    (sidebar), or **permanently delete a deck** — removes its
    mainboard/maybeboard/card tags/themes, reassigns any collection
    Locations that matched its name back to the default "Box" storage
    location (Prompt Pass 13 — it used to clear them to blank instead),
    and detaches (rather than deletes) its rows in past games so the rest
    of that game's history stays intact. Requires typing the deck's exact
    name to confirm; cannot be undone.
  - **Themes** — add/remove main/sub themes per deck, picked from a
    master theme dropdown you manage (add/remove entries in an expander;
    removing one only changes what's offered, never touches existing
    per-deck assignments).
  - **Card Tags** — bulk-edit a whole deck's mainboard tags for any
    tag_type in one table (create new tag types on the fly, e.g. Rule 0
    categories). There's no deck-level tags feature — use the Deck Info
    tab's description/combos/tutors fields for anything deck-wide instead.
  - **Mainboard** / **Maybeboard** — add a card (by name, or an exact set +
    collector number — resolves against your existing `cards` table first,
    falling back to a live Scryfall lookup only for a card the dashboard
    hasn't seen before), then bulk-edit quantities/review flags/notes or
    remove cards in one table, same pattern as Card Tags. Adding a card to
    the **Mainboard** that isn't tracked in your collection under that
    exact printing yet automatically creates a starter collection lot for
    it (quantity matching what you just added, Location set to the deck's
    name) — Deck Building Auto-Add, so the collection can't silently drift
    out of sync with what your decks actually run. Maybeboard adds don't
    trigger this (a maybeboard card is still just under consideration).
  - **Swap Manager** (Prompt Pass 13) — a deck-scoped tool for planning
    and applying mainboard upgrades. Build a queue of "Add [Card A] ->
    Replace [Card B]" entries (nothing is written to the database until
    you confirm); each queued entry shows live inventory availability for
    the card being added — whether you own it, whether a copy is sitting
    free in storage, or whether every copy you own is already sleeved in
    another one of your decks. Confirming the queue updates this deck's
    mainboard for every swap AND reconciles `collection.location`: the
    replaced card's lot(s) sleeved in this deck move back to "Box", and
    the added card is sleeved here — reusing an available owned copy if
    one exists, otherwise creating a brand-new starter lot (tagged
    "Auto-added (swap manager)", same auditability convention as Deck
    Building Auto-Add below) rather than ever pulling a physical copy out
    of another deck's box.
  - **Collection** — the Location field is a controlled dropdown (Prompt
    Pass 13): a master list of standard storage locations you manage (an
    expander, "Box"/"Lands Box" seeded by default) unioned with every
    tracked deck's own name, so "sleeved in this deck" tracking can't
    drift from a typo. Type a card name for a suggestion list drawn from
    cards already in your database (refreshes once you finish typing or
    press Enter); pick one to get a second dropdown of every printing you
    already know about (set, collector number, price), or a "🔍 Check
    Scryfall for other printings" button to pull in printings you don't
    have locally yet. Add the exact printing along with
    quantity/foil/location/price/a real date picker for Date Acquired
    (defaulting to today)/source, or skip the suggestions entirely and
    enter a Set Code + Collector Number directly for a brand-new card
    (live Scryfall lookup, same fallback as Mainboard/Maybeboard above).
    Then search and bulk-edit (Location is a dropdown here too) or remove
    existing lots.
  - **Game Changers** — every card that's either Scryfall-flagged or
    already carries one of your custom categories, across your whole
    database (not just one deck), shown with card art. Edit categories
    per card (picked from a master category dropdown you manage, same
    pattern as Themes), and see at a glance how many copies you own and
    how many of your decks currently run it.
  - **Card Data** — re-fetches every card already in your database by its
    permanent Scryfall ID and refreshes its Reserved List flag, Game
    Changer flag, Showcase/Borderless flags, Commander legality, and
    price — without the wipe-and-rebuild `migrate.py` does. Afterward it
    also automatically prunes the collection: any lot with no assigned
    location, a quantity of 0/none, and not present in any deck's
    mainboard or maybeboard is deleted. Same underlying logic (and same
    CLI-vs-dashboard sharing pattern) as Moxfield export — see
    `scripts/refresh_card_data.py` / `dashboard_lib/refresh.py`.
- **Commander Game Tracking** — log a game with up to 4 seats, each either
  a tracked deck (dropdown), a known opponent deck, or free text for a new
  one — Player works the same dropdown-or-free-text way — plus a win flag
  per seat and match notes; autofill for Player and Opponent's Deck is
  sourced from prior log entries (Prompt Pass 7). A logged game can also be
  edited in place, not just deleted and re-entered. Submitting is blocked
  unless exactly one winner is picked and every seat with any info at all
  has a resolvable deck. Two tabs: "Log & manage games" (the form, an
  actual rendered game history — not a raw table — and Edit/Delete
  pickers) and "Player & deck stats" (win-rate comparisons by deck and by
  player, a multiplayer ELO rating per player, and a head-to-head
  win-rate matrix). No "Game #" is tracked or shown anywhere — entries are
  always ordered by date, then game_id as a same-day tiebreaker. Player
  name is new as of Phase 2 — games logged before it existed simply don't
  count toward anything scoped to player (by-player win rate, ELO,
  head-to-head).

Not yet in the dashboard: the Proxy flag on the Turn 0 panel (see "Open
items" below), a discovery view of the *full* official Game Changers list
(owned vs. not-yet-owned — the Game Changers tab only shows cards already
in your database), and print-to-PDF — those remain later-phase work.

### Migration is one-way: CSV → database, not the reverse

`migrate.py` is meant for your **initial import only**. It always wipes
and rebuilds `mtg_collection.db` from scratch from the CSVs — there's no
attempt to preserve anything you've since edited in the dashboard (card
tags, deck metadata, mainboard/maybeboard, themes, collection lots). If
you re-run it after you've started managing things in the dashboard, it
will silently overwrite those edits back to whatever the CSVs say, and
it'll print a one-line warning to that effect right before it does.

**The rule going forward: once you've moved your data over, don't run
`migrate.py` again.** Manage decks, card tags, mainboard/maybeboard,
themes, and the collection directly in the dashboard's Editor page from
then on — that's now the source of truth, and there's no CSV round-trip
needed for any of it.

### Schema changes without re-migrating: additive upgrades on connect

With `migrate.py` retired as a re-runnable tool, the schema still needs a
way to grow occasionally without wiping your database.
`dashboard_lib/queries.get_connection()` — the one function everything
else connects through — checks for a short list of expected
columns/tables/views on every connect and applies a plain `ALTER TABLE`
or `CREATE TABLE`/`CREATE VIEW` if one's missing, then moves on. It never
touches existing rows, never drops anything, and is a no-op once a
database is current, so it's safe to run on every launch. This is how
`is_reserved` reached your existing database without a rebuild (Prompt
Pass 1), and how the Phase 2 additions below did the same:
`cards.is_showcase`/`is_borderless`/`edhrec_salt`,
`decks.cover_image_path`, `game_participants.player_name`, plus two new
tables (`theme_catalog`, `game_changer_category_catalog`) that get
seeded — once, the first time they're created on your database — from
whatever's already in `deck_themes`/`game_changer_tags`, so your existing
theme and category assignments show up as dropdown options immediately
rather than starting from an empty list. Any future schema addition would
follow the same pattern (see `_SCHEMA_UPGRADES` / `_SCHEMA_TABLE_UPGRADES`
/ `_SCHEMA_VIEW_UPGRADES` in `queries.py`).

I tested every menu path against a mocked environment: normal exit,
blocked options before migration, invalid input, and the missing-package
warning correctly firing only for actions that actually need pandas/
requests (confirmed Moxfield export triggers no warning at all, since
it's pure stdlib). The one thing I can't verify here is a real `pip`/
`conda install` in your actual environment, since this sandbox doesn't
have one to test against.

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

Re-running `migrate.py` deletes and rebuilds the `.db` file from scratch
every time — see "Migration is one-way" above. It'll print a warning if
a database already exists, since a re-run at that point discards any
dashboard-made edits.

## What I already validated

I dry-ran the full migration logic against your real CSVs using a mocked
Scryfall client (no live API calls, synthetic card data) to catch bugs
before handing this off. Confirmed:
- Raktres totals exactly 100 cards, matching your data
- The two decks that predate `deck_mapping.csv` (Rakdos Showstopper,
  Vorevold, Sac Master — see `FORMER_DECK_NAMES` in `migrate.py`) still
  get registered as ordinary decks, so their entries in
  `games_played.csv` link to a real `deck_id` instead of falling through
  to the free-text "unmatched opponent" path
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

## Game Changers and Reserved List — pulled straight from Scryfall

`cards.is_game_changer` and `cards.is_reserved` are both pulled straight
from Scryfall's own official fields (`game_changer` and `reserved`) at
fetch time — no manual list to maintain for either one. "At fetch time"
matters: once a card is in your database, its flags stay whatever they
were when you last fetched it — they don't silently update on their own.
If WotC revises the Bracket list or adds to the Reserved List later, your
existing cards won't reflect that until you either re-run `migrate.py`
(which, per above, wipes and rebuilds everything) or use the **Editor
page's "Card Data" tab** (or `scripts/refresh_card_data.py` from the
command line), which re-fetches every card by its permanent Scryfall ID
and updates just these fields — Reserved List, Game Changer, Commander
legality, and price — without touching anything else. Worth running
occasionally as general upkeep.

Your `game_changers.csv` (63 cards, categorized as Combo/Mana/Power/
Stax-Unfun/Tutor/Value) loads into a separate `game_changer_tags` table,
giving you the sub-category detail Scryfall's flat boolean doesn't
provide. The two are cross-checked at the end of migration — anything
where they disagree gets printed in the resolution report, e.g.:

- **In your list, Scryfall says no** — your list may be ahead of a recent
  Scryfall data update, or the card may have been reviewed off the list.
- **Scryfall says yes, not in your list** — a card WotC added that your
  categorization hasn't caught up to yet; worth adding a category tag.

There's no equivalent custom sub-categorization for the Reserved List —
Scryfall's boolean is the whole story there, shown per-deck on the Decks
& Maybeboard page's Turn 0 panel and filterable on the Collection page.

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
name-keyed pattern as Game Changers, no Scryfall calls needed. As of
Prompt Pass 12, it also has an in-dashboard editor — the Editor page's
**Mana Tags** tab, mirroring the Game Changers tab's master-catalog +
bulk-edit pattern (a `mana_tag_catalog` table backs the tag dropdown),
plus a card-name search to assign a first-ever tag to any card in the
database (mana_tags has no Scryfall-derived flag the way Game Changers
does, so nothing else seeds a brand-new candidate card). Reconstructs the
old "Optimized Mana" PDF footnote dynamically, per deck:

```sql
SELECT tag, cards FROM deck_mana_tag_summary dmts
JOIN decks d ON d.deck_id = dmts.deck_id
WHERE d.name = 'Vilis, Blood ATM';
-- Fast          | Sol Ring
-- Mana Doubler   | Bubbling Muck, Nirkana Revenant, Magus of the Coffers, Cabal Coffers
-- Medallion      | Jet Medallion
-- Ritual         | Dark Ritual
```

## Deck Swap Manager

Added in Prompt Pass 13 (Editor page, **Swap Manager** tab, deck-scoped)
to make upgrading a deck a deliberate, reviewable batch action instead of
adding/removing cards one at a time and separately remembering to fix up
`collection.location` afterward. Workflow:

1. **Swap Builder** — pick a card currently in the deck's mainboard to
   replace, and type the name of the card you want to add instead. Queue
   as many of these `Add [Card A] -> Replace [Card B]` entries as you
   want before touching the database.
2. **Inventory Checking** — each queued entry shows a live read of
   `collection` for the card being added: not owned at all, owned and
   sitting free in storage, or owned but every copy is already sleeved
   in one of your OTHER decks (named, so you know exactly where it is).
3. **Execution Confirmation** — one button applies the whole queue.
   `deck_cards` is updated for every swap, and `collection.location` is
   reconciled for both sides: the replaced card's lot(s) sleeved in this
   deck move back to `"Box"`, and the added card gets a lot sleeved in
   this deck — an existing available copy if you have one, otherwise a
   fresh starter lot (`source = "Auto-added (swap manager)"`, same
   auditability convention as the Mainboard tab's Deck Building
   Auto-Add). It will never silently move a card out of another deck's
   box to satisfy this one.

The queue itself only lives in the page session — nothing is written
until you click "Confirm & apply all queued swaps".

## Collection Location — now a dropdown

`collection.location` is still free text at the schema level (a box
label or a deck's own name — see schema.sql's comment on the column),
but as of Prompt Pass 13 the Editor no longer lets you type it freely.
A new `location_catalog` table holds your standard, non-deck storage
locations (seeded with `"Box"` and `"Lands Box"`, manage the rest in the
Collection tab's "⚙️ Manage the master Location list" expander); the
dropdown offered everywhere Location is set or edited is that catalog
unioned with every currently tracked deck's own name. Deleting a deck
now **reassigns** any collection rows whose Location matched it back to
`"Box"` (previously this cleared them to blank) — the cards still
physically exist, they've just come out of a deck that no longer exists.

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

There are two, separate Moxfield export features: a **deck** paste-format
export (one deck's list at a time) and a **collection** CSV export (your
whole collection, or a subset, as an importable .csv). Both share the
same CLI-vs-dashboard pattern via `dashboard_lib/moxfield_export.py`, so
neither can drift from the other.

### Deck export

**In the dashboard** (Decks page → "📋 Export to Moxfield"
expander, under the deck value line): shows the formatted list in a code
box with a one-click copy-to-clipboard icon (hover the box, click the
icon in the corner), plus a checkbox to include the maybeboard as a
separate section, and a "💾 Save to file" button that writes a `.txt` file
into `moxfield_exports/` in the project folder (named after the deck,
with " (with maybeboard)" appended when that box is checked, so the two
variants never overwrite each other).

**From the command line**, same output, same underlying formatting logic:

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

### Collection CSV export

**In the dashboard** (Collection page → "📋 Export to Moxfield (Collection
CSV)" expander): pick **Full Collection** or **Owned NOT in a deck**, then
either "⬇️ Download CSV" (browser download) or "💾 Save to file" (writes
into `moxfield_exports/`, e.g. `moxfield_collection_full.csv` /
`moxfield_collection_not_in_deck.csv`).

**From the command line**, same output:

```bash
python export_moxfield_collection.py                     # full collection -> stdout
python export_moxfield_collection.py --not-in-deck        # owned, not run in any deck
python export_moxfield_collection.py -o collection.csv
```

One row per printing + foil status you own — quantities and purchase
prices are summed across any collection lots sharing that exact printing
(separate lots exist elsewhere for price/date history, but Moxfield's
own CSV format expects one row per printing/foil, not one row per lot).
Untracked bulk lots (no counted quantity — e.g. a pile of unsorted
basics) are excluded, same as everywhere else "tracked" totals are shown
in this app. Column mapping: `count`/`tradelist count` (0 if that exact
printing is run in any deck)/`name`/`edition` (lowercased set
code)/`condition` (always "Near Mint")/`language` (always
"English")/`foil`/`tags` (always blank)/`last modified` (current
timestamp — there's no per-lot last-modified data to draw from)/
`collector number`/`alter` (always FALSE)/`Proxy` (always FALSE, since
there's no Proxy tracking convention in this dashboard yet — see "Known
open items" in `PROJECT_STATE.md`)/`purchase price` (summed cost paid,
blank if never recorded).

## Win/loss stats

Computed dynamically, never stored — always reflects the live game log:

```sql
SELECT * FROM deck_stats WHERE games_played > 0 ORDER BY win_rate DESC;
```

By player (only counts games logged since `player_name` existed, i.e.
Phase 2 onward):

```sql
SELECT * FROM player_stats ORDER BY win_rate DESC;
```

Two more player-scoped metrics were added in Prompt Pass 7, both computed
in Python rather than plain SQL (an ELO-style rating needs to process
games in order, one at a time, which a single aggregate query can't
express) — `dashboard_lib/queries.py`'s `player_elo_ratings(conn)` and
`player_head_to_head_matrix(conn)`. The ELO variant scores a game's
winner a win against every other seated player and every pair of
non-winners a draw against each other (a straight 1v1 ELO's 50/50
baseline doesn't fit a 4-player pod), with all pairwise deltas for one
game computed from a ratings snapshot taken at that game's start and
applied together, so results don't depend on the arbitrary order players
happen to be listed in. The head-to-head matrix is a simpler literal
win-rate crosstab between every pair of players who've shared a game.

All four views/functions back the **Commander Game Tracking** page's
"Player & deck stats" tab, which also lets you log, edit, or delete
games — existing tracked decks via dropdown, or a known/free-text
opponent deck and player name with autofill from past entries — and
browse an actual rendered game history rather than a raw table, on its
"Log & manage games" tab.

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

## Custom fields & editable data: CSV for initial import, dashboard from then on

Deck names/metadata, mainboard, maybeboard, themes, collection lots, card
tags, Game Changer categories, and (Prompt Pass 12) mana tags are all
editable directly in the dashboard (Editor page — there's no separate
Tag Editor page anymore; it was folded into Editor) — no CSV round-trip
needed for any of it. (EDHREC salt scores used to be hand-editable here
too; that feature was retired dashboard-wide — see "EDHREC Salt Score"
below.)

`migrate.py` wipes and rebuilds the whole database on every run, with no
attempt to preserve dashboard edits — see "Migration is one-way" above.
Use it for your initial CSV import, then manage everything through the
dashboard from then on.

## Next steps (not built yet)

- Proxy flag on the Turn 0 panel — still an open item, see below (needs
  the Location/Proxy convention confirmed first)
- Rule 0 tags — no data source yet (open item, see below)
- A discovery view of the *full* official Game Changers list (owned vs.
  not-yet-owned) — the Editor's Game Changers tab only shows cards
  already in your database (owned, or run in a deck), not the whole
  official list
- An in-tool editor for mana tags (currently CSV-only, unlike Game
  Changer categories which got one in this update)
- HTML/CSS print-to-PDF one-pager, styled after your existing deck
  summary PDFs
- EDHREC comparison — on the backburner per your call. The narrower
  hand-entered salt-score field that used to live in the Editor as a
  stand-in for this was itself retired — see "EDHREC Salt Score" below —
  so there's currently no EDHREC-sourced data anywhere in the dashboard.

## EDHREC Salt Score (retired)

A prior pass added a hand-maintained `cards.edhrec_salt` field, since
EDHREC salt scores aren't exposed by the Scryfall API. This was later
audited for a proper data source: EDHREC has no lightweight public API
for salt scores, and while MTGJSON does carry an `edhrecSaltiness` field,
it only ships inside MTGJSON's full-size exports (`AllPrintings` /
`AllIdentifiers` / `AtomicCards`, each hundreds of MB or more) — too
heavy an ongoing dependency to sync just for one hand-sized number in a
personal-scale, mostly-offline tool. The whole feature was removed as a
result: the Editor's Salt Scores sub-editor, the Decks page's Top 10
Saltiest panel, and every supporting query/write/loader function are
gone. `cards.edhrec_salt` itself is still physically declared in
`schema.sql`, per this project's rule of never dropping a column from a
live database, so any values entered before this change aren't lost —
there's just no read or write path to them left in the dashboard.
