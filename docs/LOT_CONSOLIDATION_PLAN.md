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
- [ ] **Phase 1**: The new merge rule, applied by every writer.
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
  identity for the move log, deck-change storage claims, `free_lots`, Undo and
  the Binder page. Only *when rows merge* changes.
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
- **An uncounted row (NULL quantity) absorbs a counted one at the same
  place.** Today only basic lands have NULL quantity: all 5 such rows are
  basics in `Lands`, and NULL means "an uncounted pile". Adding counted
  copies to a pile leaves a pile, so the result stays NULL. Two uncounted rows
  merge into one uncounted row. Today uncounted rows never merge, so this is
  new; no existing group is affected.
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
- A small helper, `_settle(conn, collection_id)`, finds the twin, merges and
  returns the surviving id. Every writer below calls it after changing a
  row's key columns. `move_lot` already does this inline, so it moves into
  the helper.

### Writers that must settle

Every path that can make a row's key match another row's:

| Writer | Today | Change |
|---|---|---|
| `move_lot` | merges, but only on the old key | uses `_settle`, so the new key applies |
| `add_collection_lot` | always inserts a new row | inserts, then settles: a new copy at an occupied place adds to that row. The move log still records the copies arriving (`from` NULL), against the surviving id. Covers hand adds, the Moxfield list paste (`order_import`, unlocated rows merge with unlocated rows), purchases and builds |
| `update_collection_lot` | no merge | settles when `location` or `foil` changes (Collection grid and the Workbench unlocated panel) |
| `set_lot_printing` | no merge | settles after re-pointing to another printing |
| `_cascade_deck_name` (`rename_deck`) | bulk `UPDATE … WHERE location=old` | settle each row it moved. A collision only happens if a storage Location already used the new name, but the index would reject it |
| `delete_deck` | bulk `UPDATE … SET location='Box'` | settle each row it moved. **This one collides often**: a deck's Sol Ring going back to Box, where a Sol Ring already sits |

Writers that return a `collection_id` return the **surviving** id, so callers
(the move pop-up, Undo lists, Binder's move) always hold a row that exists.
`move_lot` already does this.

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

### Cleanup and enforcement (Phase 2)

- **Migration** (the startup migration list in `dashboard_lib/queries.py`):
  when the index is missing, take a backup snapshot (`backup.snapshot(conn,
  "before", "consolidate-lots")`), run `consolidate_lots()` under the new
  rule, then create the index. It must consolidate first, or the `CREATE`
  fails on today's 8 groups.
- **Index**, in `schema.sql` and the migration:

  ```sql
  CREATE UNIQUE INDEX idx_collection_place
      ON collection(scryfall_id, foil, TRIM(COALESCE(location, '')));
  ```

  It's an expression index, so blank and NULL Locations count as the same
  place (SQLite treats plain NULLs as distinct in a unique index).
- **Collection page**: remove the manual "Consolidate" button
  (`pages/1_Collection.py` around line 260) and its help text, since there's
  nothing left for it to do. `consolidate_lots()` stays for the migration and
  tests.

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
- Counted plus uncounted basics at the same place leaves an uncounted row,
  and the price adds.
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

- [ ] `_find_twin` on the new key; `_merge_lots` with the older-row and
      uncounted-pile rules; the `_settle` helper.
- [ ] Settle in `add_collection_lot`, `update_collection_lot` (Location/foil),
      `set_lot_printing`, `_cascade_deck_name` and `delete_deck`; `move_lot`
      uses `_settle`.
- [ ] `revert_moves`: subtract for a merged `"new"`; update the docstring.
- [ ] Tests in `tests/test_collection_writes.py` and
      `tests/test_collection_moves.py` for the edge cases above. Update tests
      that expect two rows where there is now one (e.g. the
      `consolidate_lots` test with differing prices, and tests adding the same
      card twice to one place).
- [ ] Full test suite passes. The app runs, and nothing on the existing DB
      changes yet, because existing duplicates are only merged when touched.

### Phase 2: Clean up existing rows and enforce the rule

- [ ] Migration: snapshot, consolidate, create `idx_collection_place`.
- [ ] `schema.sql`: the index, and a new comment on `collection` (one row per
      printing, finish and Location; source, date and paid are best-effort
      labels, kept from the oldest purchase on merge).
- [ ] Remove the Consolidate button from the Collection page.
- [ ] Migration test on a fixture DB with duplicate groups, plus a run on a
      **copy** of `mtg_collection.db`, checking the numbers in **Edge cases**.
- [ ] Test-fixture `INSERT`s that create duplicates now fail. Fix those
      fixtures.

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
