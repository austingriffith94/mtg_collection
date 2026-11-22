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

- [x] **Phase 0** — Branch and plan.
- [x] **Phase 1** — Foil prices on `cards` (schema, Scryfall fetch, refresh).
- [x] **Phase 2** — Settings table and the candidate rules (`dashboard_lib/binder.py`, tested).
- [x] **Phase 3** — The Binder page: settings, fill gauge, candidate list, move to Binder.
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
- **Settings are saved.** Minimum value, the two rule switches, which storage
  Locations to scan, the binder's Location name and its capacity are stored in
  the database, so they stay put between sessions.

Agreed 2026-11-21, second pass:

- **The Binder is just storage.** Deck building treats it like any other box:
  Auto sourcing (`queries.free_lots`, biggest lot first) can take a Binder
  copy, and the source picker offers the Binder as a storage Location. There's
  no special ordering and no change to the allocator.
- **Cards leaving a deck still go to Box (or Lands).** `default_dest_for` is
  unchanged. A qualifying card that lands in Box gets flagged by the tool and
  counted on Home, and you move it into the Binder yourself.
- **Which storage Locations get scanned is a setting.** It's a checklist of
  storage Locations with all of them ticked to start, so the Lands box can be
  unticked if it's kept as a working box of lands. A Location added later
  (a new box) is scanned unless you untick it. The setting stores the
  unticked ones, not the ticked ones.
- **Cards can be dismissed as "Not for binder".** It's by card (oracle_id, so
  every printing), kept in its own small table, and hidden from the list and
  the Home count. A "Show dismissed" toggle lists them with an Undo. A
  dismissal never expires, even if the price rises a lot later. With no price
  history there's nothing to compare against, so un-dismissing is a manual
  check. This is a plain list you keep; nothing learns from it.

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

Second pass, same day:

- **Prices are three weeks old.** Every `cards` row has
  `price_updated_at = 2026-11-01`. Refresh is manual (Card Database → Card
  Data, or `run.py`) and re-fetches all 2,038 printings one at a time. "Flag
  cards that went up" only works as well as the last refresh, so the page
  shows the price date.
- **12 cards worth $2+ in storage are also on a deck list.** Two decks are
  `brewing`, and a brewing deck can have promised itself a specific stored
  copy (`queries.brewing_claims`). A pending shortlist row can also pin a
  copy to a storage Location (`source='storage'`). Moving a pinned copy to
  the Binder would leave the plan pointing at the wrong box. Auto rows don't
  care, because Auto takes from any storage.
- **Some candidates are split across lots or come in multiples.** Emergence
  Zone and Thought Vessel each sit in two stored lots. Kediss (2 × $5.27) and
  Rograkh (2 × $2.32) are 2-copy lots. Commander only needs one of each in a
  deck, so the page needs a copies-to-move count per row.
- Stored oddities the rules must cope with: one token and one card that isn't
  Commander-legal. The value rule handles both, so neither needs a special
  case.

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
  | `binder.skip_locations` | `[]` (JSON list of storage Locations *not* scanned) |

- New table `binder_dismissed (oracle_id TEXT PRIMARY KEY, name TEXT,
  dismissed_at DATE)` for "Not for binder". Like `app_settings`, it's created
  through `_SCHEMA_TABLE_UPGRADES` and added to `schema.sql`. `name` is only
  there so the Show-dismissed list can be read without a join.
- Settings validation (on the page, before `set_setting`): the binder Location
  can't be a deck name, and it's compared with `queries.same_location`
  (ignoring case and spaces), so `binder` and `Binder` are the same place. The
  minimum value is 0 or more. The capacity is 1 or more.

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
- **Planned deck adds.** A stored copy can be promised to a deck in two ways:
  a brewing deck's saved storage choice (`queries.brewing_claims`), or a
  pending shortlist row pinned to a storage Location (`source='storage'`).
  Moving that copy to the Binder would leave the plan pointing at the wrong
  box. `planned_for` reads both of these, using the existing functions rather
  than a new query, so it can't drift from what the Workbench shows. Matching
  is by card and Location. Such lots still appear, but with
  `planned_for = <deck>`, so the page can warn and leave them unticked. Auto
  rows (no source chosen) are not a claim, because Auto takes from any storage
  and is fine with the Binder.
- **Scope filters:** Locations in `binder.skip_locations` are left out, and so
  are cards in `binder_dismissed`, matched by oracle_id. Locations are compared
  with `queries.same_location`. Lots with no Location are always scanned.
- **A cheap count for Home.** `binder_candidate_count(conn)` runs the rules
  without the `planned_for` work, which pulls in the allocator's reads and is
  too heavy to run on every Home load.
- **Refresh summary.** `refresh.refresh_all_cards` counts candidates before
  and after its UPDATE pass and adds `binder_new` (names that now qualify) to
  its summary, so the Card Data tab can say "3 stored cards now qualify for
  the Binder". Both counts are taken in the same run and nothing is stored, so
  this isn't price history.
- **Copies of a card already in the Binder still appear**, with the
  `already_in_binder` count shown, so a second Dystopia is a choice you make
  rather than one made silently for you.

### The Binder page (Phase 3)

`pages/12_Binder.py`, in the **Edit** section of `nav.PAGES` (it moves lots).
Title `BINDER`, icon 📒, `url_path="Binder"`.

- **Settings expander:** minimum value (number input), the Reserved List and
  Game changer switches, a checklist of storage Locations to scan, and the
  binder Location name and capacity. Changes are validated (see Settings) and
  saved through `set_setting`.
- **Price date:** "Prices as of 2026-11-01", from the oldest
  `price_updated_at` among the stored lots, with a link to Card Database's
  Card Data tab. It is shown as a warning when the date is more than 14 days
  old. Nothing refreshes automatically.
- **Fill gauge:** `N / 720 copies in Binder`, plus how full it would be after
  moving the selected rows.
- **Candidate table:** a data editor with a tick column, thumbnail, name,
  set, foil, quantity, **copies to move** (it starts at the lot's quantity and
  can be lowered), Location, value, reasons, and the already-in-binder and
  planned-for notes. Every row starts unticked. Select all / Clear buttons
  tick or untick every row shown, and Select all skips rows with
  `planned_for`, which show a ⚠ note.
  A filter lets you narrow by current Location (e.g. only `Box`).
- **Move selected to Binder:** calls
  `writes.move_lot(conn, id, location, quantity=copies, reason="binder")` for
  each ticked lot, then reruns. A partial move leaves the rest behind as its
  own lot, and a move into the Binder merges with a matching lot already there
  (both are existing `move_lot` behaviour). The binder Location is added to
  `location_catalog` if it's missing. A warning appears, without blocking the
  move, if the move would go over capacity. Check that `reason="binder"` reads
  well in Card Lookup's History (`card_view.move_label`); if it doesn't, use
  the existing `collection_edit` reason.
- **Not for binder:** a button that dismisses the ticked rows' cards. A "Show
  dismissed" toggle lists them, each with an Undo.
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
- A Location in `skip_locations` is left out, matched without regard to case
  or spaces. A newly added storage Location is scanned by default. Lots with
  no Location are always scanned.
- A dismissed card is hidden in every printing. Undo brings it back.
- A copy claimed by a brewing deck's saved choice, or by a pinned
  `source='storage'` shortlist row, shows `planned_for`. A copy only an Auto
  row wants does not.
- Moving 1 of a 2-copy lot leaves a 1-copy lot behind at the old Location,
  and both moves are logged.
- A binder Location that matches a deck name is refused. Changing the case
  (`binder`) is treated as the same place.
- The refresh summary's `binder_new` lists only cards that didn't qualify
  before the run, and is empty on a second run straight after.

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

- Add the `app_settings` and `binder_dismissed` tables and the get/set and
  dismiss/undo helpers.
- Write `dashboard_lib/binder.py` (settings dataclass, candidates, the cheap
  count, fill), with a header comment explaining the rules.
- Add `binder_new` to the refresh summary and show it in the Card Data tab.
- Tests (`tests/test_binder.py`): every edge case above, on a small fixture
  database.

As built (2026-11-21): settings are validated and saved by
`binder.save_settings()` (raises `ValueError`, stores nothing on failure), so
the page just calls it. Dismissals are keyed by `binder.card_key()`: the
oracle_id, or `name:<name>` for a card row with none. `binder_candidate_count`
counts distinct cards, not lots (Emergence Zone in two boxes is one). Against
a copy of the live DB: 78 lots / 80 copies / 76 cards at $2, none planned for a
deck. **For Phase 3:** `"binder"` is not in `writes.MOVE_REASONS`, so
`move_lot(..., reason="binder")` raises today. Either add it there (and a
label in `card_view.move_label`) or use `collection_edit`.

### Phase 3 — The Binder page

- Write `pages/12_Binder.py` and add a `nav.PAGES` entry (the `tests/test_nav.py`
  check requires one).
- Tests: a page smoke test (`tests/test_pages_smoke.py`). An AppTest that
  ticks a row, moves it, and checks that the lot's Location became `Binder`
  and a `collection_moves` row was written. Also a browser check against a
  DB copy (see the project's browser-check memory).

As built (2026-11-21): `"binder"` was added to `writes.MOVE_REASONS`, and
History reads it as "moved to the binder". The binder Location is added to
`location_catalog` on the first **move**, not on page load, so viewing the
page writes nothing. Two read helpers were added to `binder.py`:
`prices_as_of()` and `scan_location_options()`, which is the scan checklist
(the catalog plus any non-deck Location a lot sits at, minus the binder).
Saving the settings keeps unticked Locations that aren't offered right now.
Every row starts **unticked**, because you decide (changed at the user's
request). **Select all** ticks every row shown except rows a deck has planned,
and **Clear** unticks them all. Moving or dismissing resets the table to
unticked. Browser check on a copy of
the live DB: 78 lots / 80 copies / 76 cards listed. Move all gave 80 copies
in 77 Binder lots (the split cards merged) and 78 `binder` move rows.

### Phase 4 — Nudges, docs, wrap-up

- Add the Home count line and the Collection Editor notice, with AppTests for
  both.
- Update `README.md` (a Binder section) and `PROJECT_STATE.md`. Mark this plan
  finished and move it to `docs/archive/`.

## Backlog (not planned)

- A **"could come out"** list: Binder cards that no longer pass any rule, e.g.
  after a price drop. Left out for now to keep the tool simple. It is the same
  rules run on the Binder's own lots.
- A "Binder candidates only" filter on the Collection page, and a "Binder
  candidate: reasons" badge in Card Lookup.
- Sending a qualifying card straight to the Binder when it leaves a deck, by
  changing `default_dest_for`. Turned down on 2026-11-21 in favour of flagging.
- Making Auto sourcing take Binder copies last. Turned down on 2026-11-21: the
  Binder is just storage.
- Use the finish-aware copy value in deck value and collection totals.
- A rule for cards tagged by hand ("always binder"), if that's ever needed.
- Page/slot numbers inside the binder (for physical order). Not tracked; the
  binder is a single Location.
