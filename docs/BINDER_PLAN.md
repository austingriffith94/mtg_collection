# Binder Plan — flag stored cards that belong in the binder

Working plan from the discussion of 2026-11-21. Goal: a new storage location,
**Binder** (720 slots), and a tool that lists the cards sitting in storage that
should go into it. It does two jobs:

1. **First fill.** Sorts the existing `Box` / `Lands` pile and picks out the
   valuable and notable cards.
2. **Ongoing.** Flags new cards that qualify, wherever they come from: a draft
   or purchase, a card pulled from a deck back into storage, or a stored card
   whose price has risen past the line at the last Card Data refresh.

Each phase ships on its own: the app runs, tests pass, and nothing is
half-converted at a phase boundary. Go in order. Work happens on the
`binder` branch, one commit per phase.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [ ] **Phase 0** — Branch and plan.
- [ ] **Phase 1** — Foil prices on `cards` (schema, Scryfall fetch, refresh).
- [ ] **Phase 2** — Settings table and the candidate rules (`dashboard_lib/binder.py`, tested).
- [ ] **Phase 3** — The Binder page: settings, fill gauge, candidate list, move to Binder.
- [ ] **Phase 4** — Nudges on Home and Collection Editor; docs and wrap-up.

## Decisions

Agreed 2026-11-21:

- **Cards in a deck are never candidates.** A copy whose Location is a deck
  name is ignored completely, even a game changer or a Reserved List card. It
  becomes a candidate only once it is moved back to storage.
- **Candidates come from storage.** Any Location that isn't a deck and isn't
  the Binder (today `Box`, `Lands` and blank). Basic lands are left out.
- **A card is flagged if it passes any one rule.** These are the rules:
  - **Value:** the copy is worth at least the minimum value. The default is
    **$2**, which can be changed on the page.
  - **Reserved List:** `cards.is_reserved`, whatever the price.
  - **Game changer:** Scryfall's `cards.is_game_changer`, whatever the price.
    No stored cards match today, since every game changer is sleeved. The rule
    is for when one comes out of a deck.

  Each flagged row lists the rules it passed, e.g. `$44.88 · Reserved List`.
  The Reserved List and Game changer rules can each be switched off.
- **Foil is not a rule on its own.** A foil copy is judged on its **foil
  price**. If foiling makes it valuable it passes the value rule; if not, it
  doesn't. The same goes for showcase, borderless and rarity: none of them is a
  rule, and only the price counts.
- **No price history.** Nothing stores past prices. "It went up in value" just
  means the card's current price (from the last Card Data refresh) now meets
  the minimum, so it appears in the list. Nothing records that it changed.
- **The rules are fixed and simple.** No AI, no scoring, no weights. Either a
  card passes a rule or it doesn't.
- **Moving uses the normal lot writer.** "Move to Binder" calls
  `writes.move_lot`, so every move is in the move log and shows in Card
  Lookup's History, the same as a move made anywhere else.
- **Settings are saved.** Minimum value, the two rule switches, the binder's
  Location name and its capacity are stored in the database, so they stay put
  between sessions.

## Data findings behind the design

Taken from the live database on 2026-11-21, counting stored copies that are
not basic lands and not in a deck. Prices are the non-foil prices stored today.

| Minimum value | Lots | Copies | Share of 720 |
|---|---|---|---|
| $1  | 124 | 128 | 18% |
| **$2** | **78** | **80** | **11%** |
| $3  | 50  | 51  | 7% |
| $5  | 21  | 22  | 3% |
| $10 | 5   | 5   | <1% |

- Of those copies, 432 are under $5. $2 is the default because it lets in
  enough cards to be worth sorting without filling the binder on the first
  pass. Change it on the page if the list looks too long or too short.
- Reserved List in storage: Dystopia ($44.88) and Conch Horn ($3.54). Both
  would pass the value rule anyway.
- Game changers in storage: none.
- There are 38 foil copies in storage. Today `cards.current_price_usd` holds
  Scryfall's `usd` (the non-foil price) and falls back to `usd_foil` only when a
  printing has no non-foil price. 37 of those foils are priced below $3 that
  way, so the tool can't tell whether any of them is worth binding until the
  foil price is stored. Phase 1 adds it.
- Storage is `Box` (399 copies) and `Lands` (55). Some valuable lands are in
  `Lands`, so that box counts as storage too.
- No `Binder` Location exists yet. It is added to `location_catalog` the first
  time the page runs.

## The design

### Foil price (Phase 1)

- New column `cards.current_price_usd_foil REAL`: Scryfall's `usd_foil`, or
  `usd_etched` when there's no `usd_foil`. It goes in `schema.sql`, plus a
  `_SCHEMA_UPGRADES` entry in `queries.py`. Existing rows get NULL until the
  next refresh.
- Written in the same three places as `current_price_usd`:
  `scripts/scryfall_lookup.py` (the parsed dict), `card_resolver.py` (insert
  of a new card), and `refresh.py` (the Card Data refresh UPDATE).
- **One value per copy**, used by the binder rules:
  `foil ? COALESCE(current_price_usd_foil, current_price_usd) : current_price_usd`.
  It lives in a single helper (`binder.copy_value_sql()` or a queries helper)
  so the rule is written once.
- Everywhere else (deck value, collection totals) keeps using
  `current_price_usd` for now. Switching those to the finish-aware price is in
  the backlog, not in this plan.
- After this phase ships, run **Card Database → Card Data refresh** once so
  the foil prices are filled in.

### Settings (Phase 2)

- New key/value table `app_settings (key TEXT PRIMARY KEY, value TEXT)`,
  created through `_SCHEMA_TABLE_UPGRADES` and in `schema.sql`. There's no
  settings store yet. This is a general one, so later features can use it too.
- Small `get_setting(conn, key, default)` / `set_setting(conn, key, value)`
  helpers in `queries.py` / `writes.py`.
- Keys and their defaults:

  | Key | Default |
  |---|---|
  | `binder.location` | `Binder` |
  | `binder.capacity` | `720` |
  | `binder.min_value` | `2.00` |
  | `binder.rule_reserved` | `1` |
  | `binder.rule_game_changer` | `1` |

### Candidate rules (`dashboard_lib/binder.py`, Phase 2)

- `binder_settings(conn)` reads the keys above into a small dataclass.
- `binder_candidates(conn, settings)` returns a DataFrame with one row per
  qualifying **lot**: `collection_id`, name, set and number, foil, quantity,
  Location, copy value, `reasons` (a list of the rules passed),
  `already_in_binder` (copies of the same card name already in the Binder),
  and `planned_for` (see below). It is sorted by copy value, highest first.
- `binder_fill(conn, settings)` returns the copies currently at the binder
  Location and the capacity, for the gauge.
- The rules are plain Python over one SQL pull (stored, non-basic,
  non-deck, non-Binder lots joined to `cards`). They are written so a new rule
  is one function plus one settings key.
- **Planned deck adds.** A stored lot can be claimed by a pending deck change
  (`deck_changes` with `source='storage'` and a `source_location`). Moving it
  to the Binder would leave that plan pointing at the wrong box. Such lots
  still appear, but with `planned_for = <deck>` filled in, so the page can
  warn about them and leave them unticked.
- **Copies of a card already in the Binder still appear**, with the
  `already_in_binder` count shown, so a second Dystopia is a choice you make
  rather than one made silently for you.

### The Binder page (Phase 3)

`pages/12_Binder.py`, in the **Edit** section of `nav.PAGES` (it moves lots).
Title `BINDER`, icon 📒, `url_path="Binder"`.

- **Settings expander:** minimum value (number input), the Reserved List and
  Game changer switches, the binder Location name and capacity. Changes are
  saved through `set_setting`.
- **Fill gauge:** `N / 720 copies in Binder`, plus how full it would be after
  moving the selected rows.
- **Candidate table:** a data editor with a tick column, thumbnail, name,
  set, foil, quantity, Location, value, reasons, and the already-in-binder and
  planned-for notes. Rows with `planned_for` start unticked and show a ⚠ note.
  A filter lets you narrow by current Location (e.g. only `Box`).
- **Move selected to Binder:** calls `writes.move_lot(conn, id, location)`
  for each ticked lot, then reruns. The whole lot moves; to move part of a
  lot, use the Collection page as usual. The binder Location is added to
  `location_catalog` if it's missing. A warning appears, without blocking the
  move, if the move would go over capacity.
- **Empty state:** "Nothing in storage passes the binder rules", with the
  current minimum value shown so it's clear why.

### Nudges (Phase 4)

- **Home:** a line in the pending-work area, `N cards in storage could go in
  the Binder`, linking to the page. It is hidden when N is 0.
- **Collection Editor:** after a lot is added by hand (draft and purchase
  pickups), a short notice when that card passes a binder rule, e.g. "Dystopia
  qualifies for the Binder (Reserved List)". It is only a notice. The lot is
  still saved to the Location you chose.
- Cards pulled out of decks into storage need no hook of their own. They just
  show up in the list and in Home's count.

## Edge cases the checks must cover

- A lot in a deck's Location is never listed, even if it's a Reserved List card
  or a game changer.
- A lot already at the binder Location is never listed.
- Basic lands are never listed.
- A foil lot uses `current_price_usd_foil` and falls back to
  `current_price_usd` when the foil price is NULL. A non-foil lot ignores the
  foil price.
- A card with no price at all can still pass the Reserved List or Game
  changer rules, and never passes the value rule.
- Changing `binder.location` makes the old Binder count as plain storage, so
  its cards come back as candidates. The page says so next to the setting.
- A lot claimed by a pending `source='storage'` deck change shows its
  `planned_for` deck.
- A NULL-quantity lot counts as one copy (the collection's existing rule).
- Turning off both switches with a very high minimum gives an empty list and
  no errors.

## Phases

### Phase 0 — Branch and plan

Create the `binder` branch and commit this file.

### Phase 1 — Foil prices

- Add `current_price_usd_foil` to the schema and as a `_SCHEMA_UPGRADES`
  column.
- Set it in `scryfall_lookup.py`, `card_resolver.py` and `refresh.py`.
- Tests: the parser picks `usd_foil`, then `usd_etched`, then NULL. The refresh
  UPDATE writes the column. An older database gains the column through
  `ensure_schema`.
- Afterwards, run the Card Data refresh against the real database (back it up
  first).

### Phase 2 — Settings and rules

- Add the `app_settings` table and the get/set helpers.
- Write `dashboard_lib/binder.py` (settings dataclass, candidates, fill), with
  a header comment explaining the rules.
- Tests (`tests/test_binder.py`): every edge case above, on a small fixture
  database.

### Phase 3 — The Binder page

- Write `pages/12_Binder.py` and add a `nav.PAGES` entry (the `tests/test_nav.py`
  check requires one).
- Tests: a page smoke test (`tests/test_pages_smoke.py`). An AppTest that
  ticks a row, moves it, and checks that the lot's Location became `Binder`
  and a `collection_moves` row was written. Also a browser check against a
  DB copy (see the project's browser-check memory).

### Phase 4 — Nudges, docs, wrap-up

- Add the Home count line and the Collection Editor notice, with AppTests for
  both.
- Update `README.md` (a Binder section) and `PROJECT_STATE.md`. Mark this plan
  finished and move it to `docs/archive/`.

## Backlog (not planned)

- A **"could come out"** list: Binder cards that no longer pass any rule, e.g.
  after a price drop. Left out for now to keep the tool simple. It is the same
  rules run on the Binder's own lots.
- Use the finish-aware copy value in deck value and collection totals.
- A rule for cards tagged by hand ("always binder"), if that's ever needed.
- Page/slot numbers inside the binder (for physical order). Not tracked; the
  binder is a single Location.
