# Lot Consolidation Plan: one collection row per card, finish and Location

Working plan from the discussion of 2026-11-22. Goal: stop the `collection`
table fragmenting. After this plan, a row means "my copies of this printing,
in this finish, at this Location", not "one purchase". Moves, adds and edits
that land on a place already holding that card fold into the row that's
there, so rows never pile up and there's no manual upkeep.

Each phase ships on its own: the app runs, tests pass, and nothing is
half-converted at a phase boundary. Go in order. Work happens on the
`lot-consolidation` branch (cut from `main`), one commit per phase.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [x] **Phase 0**: Branch and plan.
- [x] **Phase 1**: The new merge rule, applied by every writer.
- [ ] **Phase 2**: Clean up the existing rows and enforce the rule in the schema.
- [ ] **Phase 3**: Docs and wrap-up.

## Why

A row only merges with another today when printing, finish, Location,
**source and date acquired** all match (`writes._find_twin`). Every partial
move splits a row (`writes.move_lot`). The moved part stays separate unless
the place it lands already holds a copy from the same order on the same day,
which almost never happens. So splits never rejoin, and keeping the table
tidy means running "Consolidate" on the Collection page by hand. That only
helps when source and date match anyway.

Per-purchase tracking isn't worth that upkeep. Price paid, source and date
stay as best-effort labels on the row, but they no longer keep two rows
apart.

## Decisions

Agreed 2026-11-22:

- **Merge key: printing (`scryfall_id`), `foil`, Location.** Location is
  compared trimmed, with blank equal to NULL (the existing `_loc` rule).
  Source, date and price paid are no longer part of the key.
- **Keep the lot model; don't drop it.** `collection_id` stays the row's
  identity for the move log, `free_lots`, Undo and the Binder page. (Storage
  claims by pending deck changes are computed live from the rows, not stored
  by id, so they're unaffected.) Only *when rows merge* changes. The move log
  keeps no foreign keys, so history rows may name a `collection_id` that a
  merge has since deleted; Card Lookup reads history by card name, so that's
  harmless.
- **Location is compared trimmed and case-sensitive**, as `_find_twin` does
  today. `queries.same_location` is case-insensitive for storage names, but
  the real data has no case variants today, and the Location dropdowns
  normalise new entries. SQLite's `LOWER()` is ASCII-only, so a case-folded
  index could disagree with Python.
- **What a merge keeps:**
  - `quantity`: the two counts add.
  - `price_paid`: the two amounts add, and a NULL side counts as nothing.
    This is unchanged, and it keeps the Decks "amount paid" and per-card
    paid totals right.
  - `date_acquired` and `source`: taken together from the row with the
    **earlier** date, as the oldest purchase. A NULL date counts as later
    than any real date, and a tie keeps the lower `collection_id`'s values.
    The other row's source and date are dropped.
  - `collection_id`: the lower id survives. This is unchanged.
- **Uncounted rows ("NULL or 0" quantity, as everywhere else in the code).**
  Today all 5 such rows are basic lands in `Lands`. Basic lands are generic
  piles, so an uncounted basic absorbs counted copies at the same place and
  stays uncounted, and two uncounted basics merge into one. A **non-basic**
  card with no count is "one copy" (`COALESCE(NULLIF(quantity,0),1)`), so it
  merges as **1** and the result is counted. If the pile won there, a
  3-copy row plus an uncounted row would silently lose two copies. Today
  uncounted rows never merge, so this is new; no existing group is affected.
- **Splits still happen, and that's correct.** Moving 1 of 3 copies leaves 2
  behind and puts 1 somewhere else. Those are two places, so two rows. The
  change is that the moved copy now always merges into what's at the
  destination.
- **Enforced by a unique index**, so a write path missed here fails loudly
  instead of quietly fragmenting again.
- **Columns stay.** `date_acquired`, `source` and `price_paid` stay in the
  schema and the Collection editor, as labels for the row.

## Data findings behind the design

From `mtg_collection.db` on 2026-11-22:

- 1,331 rows hold 1,359 copies. Only 21 rows have a quantity above 1, so
  nearly every row is a single copy.
- 8 groups share printing, finish and Location, 17 rows in all. Phase 2
  collapses them to 8 rows:

  | Card | Location | Rows (qty · source · date · paid) |
  |---|---|---|
  | Fellwar Stone | Box | 3 × (1 · TCG · 2022-05-18 · $0.58) |
  | Sol Ring | Box | 1 · CK · 2022-02-27 · $2.29; 1 · CK · 2022-02-27 · $1.99 |
  | Emergence Zone | Lands | 2 × (1 · CK · 2022-02-27 · $0.99) |
  | Exotic Orchard | Lands | 1 · CS · 2021-12-14 · $0.49; 1 · TCG · 2022-12-27 · $0.24 |
  | Arcane Signet | Box | 1 · TCG · 2022-07-09 · $0.89; 1 · Precon 1 Nalia · 2022-08-14 · — |
  | Crypt Ghast | Ayara, Blood Wine Mom | 1 · TCG · 2022-05-17 · $8.90; 1 · Auto-added (deck edit) · 2026-10-25 · — |
  | Viscera Seer | Box | 1 · CK · 2021-12-31 · $0.39; 3 · CMR 1 · 2022-03-26 · — |
  | Thought Vessel | Box | 1 · CS · 2021-12-03 · $2.99; 1 · CMR 1 · 2022-03-26 · — |

  Fellwar Stone and Emergence Zone already match under the *current* rule.
  They exist only because nobody ran Consolidate, which is the upkeep this
  plan removes.
- No group mixes a counted row with an uncounted one.

## The design

### Merge rule (`dashboard_lib/writes.py`, Phase 1)

- `_find_twin(conn, collection_id)`: match on `scryfall_id`, `foil`, and
  trimmed Location (blank == NULL). Drop the `date_acquired`/`source` terms
  and the `quantity IS NOT NULL` filter, because uncounted rows now merge too.
- `_merge_lots(conn, keep_id, drop_id)` applies the rules in **Decisions**:
  quantities add (NULL wins), price adds, date and source come from the older
  row, and the lower id is kept. As now, it writes no move-log row and doesn't
  commit.
- **Merge before writing, not after.** The unique index (Phase 2) rejects
  the *intermediate* state "UPDATE this row's Location to where a twin
  already sits", so "write, then find the twin and merge" would raise
  `IntegrityError` as soon as the index exists. Every writer instead looks up
  the occupant of the destination key **first** and, when there is one, folds
  into it rather than writing a duplicate. One helper does it:
  `_place(conn, collection_id, *, location=None, foil=None, scryfall_id=None)`
  returns the id of the row that now holds the copies (the occupant, or the
  row itself when the destination is empty). `move_lot`'s existing inline
  merge moves into it. Phase 1's tests also run with the index created, so
  this is proven before Phase 2 relies on it.
- **Swaps.** Two rows exchanging places in one save (Box<->Gisa for the same
  card and finish) pass through a collision. **Decided in Phase 1: refused**
  with a clear `ValueError` before anything is written ("these lots trade
  places … change their quantities instead"). A chain that only *looks*
  like a collision (A leaves Box for Binder, B moves into Box) is ordered
  so A goes first, and merges nothing.
- New-copy writes (`add_collection_lot`, `move_lot`'s split) go through a
  sibling helper, `_deposit(conn, scryfall_id, foil, location, fields)`,
  which folds into the occupant or inserts.
- **Races.** Two sessions can both see an empty destination. The index turns
  that into `IntegrityError`; the writers catch it once, re-read the
  occupant and retry, instead of letting a page crash.

### Writers that must settle

Every path that can make a row's key match another row's:

| Writer | Today | Change |
|---|---|---|
| `move_lot` | merges, but only on the old key; the split `INSERT` would also collide | goes through `_place` for both the whole-row move and the split-off part |
| `add_collection_lot` | always inserts a new row | `_place`-style upsert: a new copy at an occupied place adds to that row. The move log still records the copies arriving (`from` NULL), against the surviving id. Covers hand adds, the Moxfield list paste (`order_import`, unlocated rows merge with unlocated rows), purchases and builds |
| `update_collection_lot` | no merge | when `location` or `foil` changes, goes through `_place` (Collection grid and the Workbench unlocated panel) |
| `set_lot_printing` | no merge | `_place` with the new `scryfall_id` |
| `_cascade_deck_name` (`rename_deck`) | bulk `UPDATE … WHERE location=old` | per-row `_place`, no bulk UPDATE. A collision only happens if a storage Location already used the new name, but the index would reject it |
| `delete_deck` | bulk `UPDATE … SET location='Box'` | per-row `_place`, no bulk UPDATE (the loop that logs each move already exists). **This one collides often**: a deck's Sol Ring going back to Box, where a Sol Ring already sits. Today's bulk UPDATE would raise `IntegrityError` on the first one |

These are the only writers: a search of the code for `UPDATE collection` and
`INSERT INTO collection` finds nothing else outside tests and the old
`_test_*_offline.py` scripts.

Writers that return a `collection_id` return the **surviving** id, so callers
(the move pop-up, Undo lists, Binder's move) always hold a row that exists.
`move_lot` already does this.

### Batches holding ids that a merge deletes

A merge deletes a row, so anything that loops over ids captured earlier can
hit a row that is gone. Each of these needs an explicit rule:

- **`bulk_update_collection`** (Collection grid save, Workbench "Save
  locations"): it reads `current` up front, then applies edits one by one. A
  row merged away by an earlier edit would be updated as a silent no-op, or
  deleted by an old `_delete` flag. Rule: apply plain field edits (quantity,
  price, date, source, delete) **first**, then the key-changing edits
  (Location, foil) last through `_place`, and keep a `{dropped_id: kept_id}`
  map so any later edit for a dropped id is redirected to the survivor.
  *As built:* deletes and rows keeping their place go first; each
  place-changing row then goes as one `update_collection_lot` call (its
  field edits and its new place together), ordered so it waits for any
  other moving row still at its destination (`_settle_order`). With that
  order a merge only ever removes a row that is already done, so the
  dropped-id map turned out unnecessary.
- **Collection page printing edits.** The save loop calls `set_lot_printing`
  *then* `bulk_update_collection`. A row whose Set/# and quantity both
  changed would have its printing merged away and the quantity edit lost.
  Rule: field edits first, printing change last, same as above.
  *As built:* the page passes printing changes into the same save,
  `bulk_update_collection(conn, updates, printings={id: scryfall_id})`,
  so they're ordered with the Location and foil changes, and a refused
  save now leaves printings unchanged too.
- **Edits to two rows that merge with each other.** Moving A onto B's place
  while also typing a new quantity for B: the quantity edit applies to B
  first, then A's copies are added on top. Say so in the Collection page's
  help text.
- **Import follow-ups.** `order_import` returns one id per line and the
  "sleeve into a deck waiting on a buy" offer moves a given *quantity* from
  it, so lines for one card sharing a surviving row is fine, but the offer
  must keep passing the bought quantity, never the whole row.
- Loops that already re-read each lot (`fix_location_mismatch`) or take from
  distinct rows (`sleeve_into_deck`, `unsleeve_from_deck`, Binder's move)
  need no change, but each gets a test that merges mid-loop.

### Undo (`revert_moves`)

- A `"move"` entry calls `move_lot(surviving_id, from, quantity)`. That splits
  the copies back out and they merge at the origin. This already works and
  gets better: the docstring's "stays as two lots after the undo" note goes
  away.
- A `"new"` entry today deletes the row if its quantity still equals the
  added quantity. After a merge the row holds more than was added. Change it:
  if the row still holds at least `quantity` copies at `to`, **subtract**
  them (delete the row only when that reaches zero) and log it as "deleted".
  Otherwise skip with the existing message. An uncounted (NULL) row can't be
  subtracted from. Skip it with "merged into an uncounted pile, so it was
  kept".
- **Price on that subtract.** The move entry doesn't carry what was paid, and
  the row's `price_paid` now includes it. Subtract `price_paid * removed /
  have` (the same proportional rule `move_lot` uses for a split), so Undo of a
  purchase doesn't leave the money behind. A row with several mixed or NULL
  prices is only an average to begin with, which we accept.

### Cleanup and enforcement (Phase 2)

- **The one-time merge is an explicit step, not part of startup.**
  `queries.ensure_schema()` runs on every connection (pages, tests, the
  refresh script) and is deliberately additive and non-destructive. It also
  can't call `writes.consolidate_lots()` without a circular import
  (`writes` imports `queries`). So:
  1. Take `backup.required_snapshot(conn)` (it aborts if the backup fails,
     as `delete_blank_lots` does), then run `consolidate_lots()` under the new
     rule. Wire it to the Collection page's Tidy lots panel for this release
     (a one-shot button that disappears once nothing is left to merge), and
     run it once on the real DB myself during Phase 2. It must run before the
     index exists, or the `CREATE` fails on today's 8 groups.
  2. `ensure_schema()` then does `CREATE UNIQUE INDEX IF NOT EXISTS`
     inside a `try`: if duplicates are still present it logs a warning and
     skips the index (the app keeps working) instead of crashing every page.
     Fresh DBs built from `schema.sql` get the index directly.
  Real DB, checked: SQLite 3.53 (expression indexes are fine), no blank or
  unlocated rows, no location case variants, so nothing blocks the index
  after step 1.
- **Index**, in `schema.sql` and the migration:

  ```sql
  CREATE UNIQUE INDEX idx_collection_place
      ON collection(scryfall_id, foil, TRIM(COALESCE(location, '')));
  ```

  It's an expression index, so blank and NULL Locations count as the same
  place (SQLite treats plain NULLs as distinct in a unique index).
- **Collection page**: once the one-time merge has run, remove the manual
  "Consolidate" button (`pages/1_Collection.py` around line 260) and its help
  text, since there's nothing left for it to do. `consolidate_lots()` stays
  for tests and as a repair tool.
- **Blank rows and the index.** Two blank rows (no quantity, no Location) of
  the same card and finish share a key, so a path that can create one
  (`move_lot`'s Undo with `allow_blank`, a raw insert) would hit the index.
  Real data has none today. `_place` handles them like any other row (they
  merge), and `prune_collection` still removes them.

## Edge cases the checks must cover

- Moving the whole of a row into a place holding a twin leaves one row with
  the summed quantity and price, and the older row's date and source.
- Moving part of a row: the remainder stays as its own row at the old place,
  and the moved part merges at the destination.
- A move between finishes or printings never merges (different key).
- Adding a copy to a place that holds it updates that row and logs the
  arrival against the surviving id.
- An import of a card already sitting unlocated merges with that unlocated
  row.
- Deleting a deck whose card is also in Box leaves one Box row.
- Editing a row's Location in the Collection grid to an occupied place merges
  it, and the grid re-renders without the old row.
- Changing a row's printing to one already at the same place merges it.
- Counted plus uncounted **basics** at the same place leaves an uncounted
  row, and the price adds. Counted plus uncounted **non-basic** counts the
  uncounted one as 1 and stays counted. A quantity of 0 counts as uncounted.
- A save that changes a row's Set/# and quantity, where the new printing
  merges it into another row, keeps both edits.
- A save that moves A onto B's place and also edits B merges once, in the
  order the batch rule says, and drops no edit.
- A merge in the middle of `unsleeve_from_deck`, `sleeve_into_deck` and the
  Binder move loop leaves the right totals.
- A rename or delete of a deck whose cards are also in Box merges rather
  than raising `IntegrityError`, and every move is still logged.
- Two sessions placing the same card at one empty place leave one row.
- Undo of a move that merged puts the copies back and they merge at the
  origin.
- Undo of an add that merged subtracts. Undo of an add whose row has since
  shrunk below the added count is skipped.
- The migration on a copy of the real DB collapses the 8 groups to 8 rows,
  keeps 1,359 copies, keeps total `price_paid` unchanged, and creates the
  index. Running it again does nothing.
- A raw `INSERT` of a duplicate row raises `IntegrityError` once the index
  exists.

## Phases

### Phase 0: Branch and plan

- [x] Cut `lot-consolidation` from `main`; commit this plan.

### Phase 1: The new merge rule, applied by every writer

- [x] `_find_twin` on the new key; `_merge_lots` with the older-row and
      uncounted rules (basics vs non-basics, 0 == NULL); the `_place` helper
      (merge before write, retry once on `IntegrityError`).
- [x] `_place` in `add_collection_lot`, `update_collection_lot`
      (Location/foil), `set_lot_printing`, `_cascade_deck_name` and
      `delete_deck` (per-row, no bulk UPDATE); `move_lot` uses it for both
      the whole-row move and the split.
- [x] `bulk_update_collection` and the Collection page save loop: field edits
      first, key changes last (ordered; see **Batches** for why no
      dropped-id map was needed).
- [x] `revert_moves`: subtract (copies and proportional price) for a merged
      `"new"`; update the docstring.
- [x] Tests in `tests/test_collection_writes.py` and
      `tests/test_collection_moves.py` for the edge cases above. Update tests
      that expect two rows where there is now one (e.g. the
      `consolidate_lots` test with differing prices, and tests adding the same
      card twice to one place).
- [x] Run the writer tests against a schema that already has the unique
      index, so merge-before-write is proven before Phase 2 ships it. Both
      test modules run every test twice (`place_index` fixture), and the
      index SQL lives in `queries.PLACE_INDEX_SQL` for Phase 2. A one-off run
      of the whole suite with the index forced on found no missed writer;
      the only failures were raw test-fixture `INSERT`s (listed in Phase 2).
- [x] Full test suite passes. The app runs, and nothing on the existing DB
      changes yet, because existing duplicates are only merged when touched.

### Phase 2: Clean up existing rows and enforce the rule

- [ ] The one-time merge: required snapshot, `consolidate_lots()` (run on
      the real DB, and offered once on the Collection page). Then
      `ensure_schema()` creates `idx_collection_place` in a `try`, skipping it
      with a warning if duplicates remain.
- [ ] `schema.sql`: the index, and a new comment on `collection` (one row per
      printing, finish and Location; source, date and paid are best-effort
      labels, kept from the oldest purchase on merge).
- [ ] Remove the Consolidate button from the Collection page.
- [ ] Migration test on a fixture DB with duplicate groups, plus a run on a
      **copy** of `mtg_collection.db`, checking the numbers in **Edge cases**.
- [ ] Test-fixture `INSERT`s that create duplicates now fail. Fix those
      fixtures: `tests/test_pages_smoke.py` (the blank-row cases around lines
      1679-1704 insert several rows for one card with no Location),
      `tests/test_contention.py` (`_lot` helper, used by
      `test_contention_no_shortfall_when_owned_covers_demand`),
      `tests/test_plan_availability.py` (`_lot` helper, used by
      `test_unlocated_lot_summary_counts_only_lots_with_no_location`) and
      `tests/test_collection_writes.py` (the consolidate test already skips
      under the index; it needs its duplicates). The old
      `scripts/_test_*_offline.py` scripts also insert duplicates
      (`_test_prompt_pass6_offline.py` adds two unlocated rows of one
      printing). Fix them or retire them, whichever is still in use.

### Phase 3: Docs and wrap-up

- [ ] Update the docstrings that describe the old rule: `move_lot`,
      `_find_twin`, `consolidate_lots`, `revert_moves`, the
      `pages/1_Collection.py` header.
- [ ] `PROJECT_STATE.md`: the collection model line.
- [ ] Mark the plan finished, move it to `docs/archive/`, and add its row to
      `docs/archive/README.md`.

## Backlog (not planned)

- Rename "lot" to "entry" or "copies" in UI text. It's cosmetic and separate
  from the data fix.
- A per-purchase history table, if per-copy provenance is ever wanted again.
  Purchases would be logged there and the `collection` row would stay the
  single physical count.
