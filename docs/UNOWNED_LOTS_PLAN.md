# Unowned Cards Plan — the Collection only holds cards you own

Working plan from the discussion of 2026-11-01, revised the same day after
checking it against the code and a copy of the live database. Goal: a card
you don't own never appears in the Collection and never counts as a copy
you could sleeve. If it is on a deck's list it goes on the buy list by
itself. Deck stats and previewers keep working.

Each phase ships on its own: the app runs, tests pass, and nothing is
half-converted at a phase boundary. Go in order. Phase 3 is the one that
changes what you see in the Collection, and it comes after Phases 1–2 so the
buy list is ready for the cards it receives.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [ ] **Phase 0** — Clean starting point.
- [ ] **Phase 1** — Stop creating lots for cards you don't own.
- [ ] **Phase 2** — Buy list: split decks from ideas, Bought with a printing picker.
- [ ] **Phase 3** — Remove the blank lots (preview, backup, confirm).
- [ ] **Phase 4** — Clean up checks and the "sleeved of N" caveat.
- [ ] **Phase 5** — Docs and wrap-up.

## The design

**A card you don't own has no `collection` row at all.** Its data already
lives in `cards`, which is the "somewhere else unseen". `deck_cards` and
`deck_changes` point at `cards` directly.

This replaces the first draft's "keep blank lots and hide them". That draft
needed an unowned filter in about 30 direct `collection` queries, and any
query that missed it would quietly count a card you don't own. Removing the
rows makes every one of those correct with no change. It also fits what the
app already does: `prune_collection()` uses the same blank test and deletes
such lots on every Card Data Refresh, and `source='buy'` already means "on
the list, no lot".

## Decisions

Agreed 2026-11-01:

- **Unowned means** quantity blank or 0 **and** location blank. A lot with
  either one filled in is owned.
- **Basic lands are exempt** from every rule here (`is_basic_land()`). They
  can stay visible and need no quantity.
- **No reveal toggle.**
- **A new lot needs a quantity or a location.** The missing one is filled in
  (quantity 1, location "Box", `writes.DEFAULT_LOCATION`) with a message.
- **Bought lets you choose the printing.** It never assumes the deck's listed
  printing is the one you bought.
- **A deck missing cards must not read 100% built.** Intended; Phase 4 adds a
  caveat rather than changing the rule.
- **`scripts/migrate.py` is out of scope.** It is not updated for this work.

Waiting on your answer (the plan assumes the recommendation):

1. **Delete blank lots rather than hide them** (the design above). Recommended.
2. **Ideas on the buy list.** The buy list already includes an open idea whose
   card you own zero of; blank lots have been suppressing them. Recommended:
   a separate collapsed Ideas section, left out of the main total.
3. **Mark "A Refined Grizzly of Noble Heritage" as brewing.** It holds 54 of
   the 58 on-list blank lots but is currently a normal built deck.
   Recommended: yes.
4. **Where the integrity checks live.** There is no Data Integrity page.
   Recommended: Card Database → Card Data → Clean up.
5. **Imports** (separate from this plan). The app has no Moxfield paste import
   (only exports), and the order CSV import is still live on the Collection
   Editor. Build one, remove the other, or leave both for a later change?

## What the database holds today

Read-only snapshot, 2026-11-01. It moves: Card Data Refresh prunes some blank
lots, and the count dropped from 147 to 132 during this discussion.

| | Lots |
|---|---|
| All lots | 1468 |
| Blank (no quantity, no location) | **132** |
| — on a deck's list | 58 (54 Grizzly, 2 Tuvasa, 2 Ayara, 1 Raktres, 1 Marchesa) |
| — open Shortlist ideas, on no deck list | 74 |
| Blank lots that are basic lands | 0 |
| Blank lots with a price paid, date, or move history | 0 |
| Blank quantity with a location | 7 (5 basics in "Lands", 2 in Gisa) |

Every blank lot is accounted for by a deck or an idea, and none holds data
that only exists on that row. Six have a real lot of another printing, so
they stay owned.

Deleting all 132 on a **copy** of the database, with today's code:

| | Before | After |
|---|---|---|
| Buy list rows | 26 | 154 |
| — needed by decks (`contention`) | 15 | 69 ($185.03) |
| — ideas | 11 | 85 ($1,157.19) |
| Total shown | $259.46 | $1,342.22 |
| Unlocated lots | 132 | 0 |
| Grizzly, not owned | 0 | 52 |
| Grizzly, "location unknown" | 54 | 0 |
| Grizzly, in other decks | 13 | 15 |

Grizzly stays 3 / 83 sleeved. The other 11 decks' sleeved counts don't change.
The test suite passes today (719).

## Phase 0 — Clean starting point

- `pages/4_Deck_Editor.py` has uncommitted changes (an add-to-Shortlist
  button). Commit or set them aside so this work stays separate.
- Work on a branch, `unowned-cards`, one commit per phase.
- Run the suite and note the count.

## Phase 1 — Stop creating lots for cards you don't own

**Auto on a card you don't own** (`writes.sleeve_into_deck()`, ~line 1231): make
no starter lot. Return the existing `"buy"` placeholder move, as `source='buy'`
does. This covers direct edits, Board adds, and applying a planned swap, which
all go through this function.

**Undo:** `"buy"` is already in `PLACEHOLDER_MOVE_KINDS`, so `revert_moves()`
skips it. Its `"new"` branch stays, because `record_purchase()` and older
pop-ups still use it. Add a test: add an unowned card, undo, no list entry and
no lot.

**On the buy list:** `awaiting_buys()` also counts a listed card when no copy of
it exists anywhere, not only when its add was `source='buy'`. Those rows then
have a deck target, so Bought can act on them. A card owned elsewhere stays a
`contention` row.

**Required fields:**

- `add_collection_lot()`: a non-basic lot with neither field is refused; with
  only one, the other is filled. The Collection Editor's Add form shows what was
  filled in.
- `update_collection_lot()` and `bulk_update_collection()`: refuse an edit that
  would blank both on a non-basic lot, with a message.

**Wording:**

- `deck_board.py` ~719 "added a starter collection lot"
- `formatting.py` ~603 "Auto — new copy record (not owned)"
- the `buy_list()` and `_lots_for_card()` docstrings

Tests that assert a starter lot change here (`test_edit_flow.py`,
`test_deck_changes_writes.py`, and others).

## Phase 2 — Buy list: split decks from ideas, Bought with a printing picker

**Layout (Workbench):** "Needed by decks" first, with its own total; a collapsed
"Ideas" section after it, outside that total. Without this, $1,157 of maybes
buries the $185 of cards decks actually need.

**Bought:** extend the existing control (`pages/8_Workbench.py` ~507) rather than
adding a second one, and add a `scryfall_id` choice to `record_purchase()`.

| Field | Default |
|---|---|
| Printing | The deck's listed printing; pick any known one, or type set + number (`card_resolver.resolve_or_fetch_card`) |
| Quantity | The number still short |
| Foil | Off |
| Price paid | Blank (optional, as today) |
| Location | The deck, since a bought card lands in its sleeves today; can be changed to "Box" |

If the printing differs from the deck's, switch the list entry to match with
`use_sleeved_printing()` or the same logic, so it doesn't appear as a printing
mismatch in Reconcile straight away.

The order import's buy-list offer (`buy_matches()`) keeps working with no change
needed. No placeholder lots need filling in, because none exist.

## Phase 3 — Remove the blank lots

1. Clean up panel gets a **Blank lots** preview: each lot, with the deck or idea
   holding it.
2. You confirm. `backup.snapshot()` runs first, as `prune_collection()` does.
3. The lots are deleted. No move-log rows: they were never physical copies, and
   Card Lookup's History shouldn't show a removal for a card you never owned.
4. `prune_collection()` drops its deck and idea exemptions, so a Refresh clears
   any blank lot that later appears. Basics stay exempt. Its `prune` move-log row
   and the matching tests go.
5. Clear the cached loaders.

Before running it on the real database: repeat the deletion on a copy, render
every page (AppTest smoke tests), and check the deck board in headless Edge.

If you mark Grizzly as brewing, do it here (one click, `set_build_state()`).

Expected afterwards:

- Workbench's unlocated panel is empty, because every unlocated lot was a blank
  lot. The same goes for "Owned, location unknown" and the Collection's "No
  location only" toggle. They stay in the code; a real copy can still lack a
  location through an edit.

## Phase 4 — Clean up checks and the "sleeved of N" caveat

**Clean up panel, two new read-only checks:**

- **No location:** lots with a quantity but no location.
- **No quantity:** non-basic lots with a location but no quantity (today, the two
  Gisa lots).

**Deck caveat:** `deck_sourcing_plan()` already returns `located` and `needed`, and
the deck summary line already shows "% built". Show "3 of 83 sleeved · 52 to buy"
beside it, wherever that line appears, whenever a deck isn't fully sleeved.

## Phase 5 — Docs and wrap-up

- New tests not already covered above: a blank-lot-free buy list end to end,
  Bought with a different printing, the Clean up checks, and the caveat.
- Update `README.md` and `PROJECT_STATE.md`, and the `collection.quantity` comment
  in `schema.sql` (comment only, no schema change).
- Tick the phases, add an "As built" note to each, and move this file to
  `docs/archive/`.

## Not changing

- **Schema:** none.
- **Direct `collection` queries:** correct once the rows are gone, so no filters.
- **`_lots_for_card()`:** still counts a blank quantity as one copy. That is right
  for a lot with a location, and now the only kind left.
- **Art, prices and refresh:** `deck_cards_dataframe()` reads `cards`, and
  `_sleeved_art_sql()` only looks at sleeved lots. `refresh_all_cards()` covers
  every `cards` row. `sync_images.py` keeps art for deck cards and open ideas.
