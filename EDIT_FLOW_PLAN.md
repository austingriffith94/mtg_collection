# Edit Flow Plan — one way to change a deck, and you choose where cards come from

Working plan from the review of 2026-10-13, following the finished
[VISUAL_DECK_PLAN.md](VISUAL_DECK_PLAN.md). Goal: an edit made anywhere has
the same effect everywhere. When a card joins a deck you say where it comes
from (storage, another deck, or a planned buy); when it leaves, you say
where it goes; and every page that shows that card agrees afterwards.

Each phase ships on its own: the app runs, tests pass, and nothing is
half-converted at a phase boundary. Phase 0 fixes inconsistencies that
exist today and changes no editing behavior. Phase 1 has no schema change
and fixes the edit behavior people hit today; Phases 2–4 each add one
source option; Phase 5 uses all of them for building new decks and trims
the duplicate editing surfaces. Phase 6 gives collection-side edits the
same tracking. It depends only on Phase 1, so do it right after Phase 1
(recommended) or at any later point. Otherwise, go in order.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done

- [x] **Phase 0** — Fix what's already out of sync: deck renames and deletes, one finish-an-edit helper, quantity changes in history, the combo staleness warning. *Done 2026-10-13: every database write on every page ends in `card_view.finish_edit()` (or `stash_edit()` from a widget callback); see "As built" at the end of the phase.*
- [ ] **Phase 1** — One write path for every deck edit; physical copies move right away; a pop-up says what moved.
- [ ] **Phase 2** — Choose where a card comes from (storage location) and where a removed card goes.
- [ ] **Phase 3** — Take a card from another deck, as a linked pair that shows on both decks.
- [ ] **Phase 4** — Planned buys: a card can be on a deck's list while you wait to buy it.
- [ ] **Phase 5** — Building new decks from those choices, and one place per job.
- [ ] **Phase 6** — Collection-side edits tracked and flowing back: a move log for every copy, and Collection page / order import edits that touch a deck offer to update its list.

**Decided (2026-10-13):** a direct edit (Board, add bar, Mainboard table)
moves the physical copy **right away**, the same way applying a planned
swap already does, and the app shows a pop-up naming every copy it moved.
Reconcile then only flags real drift, not every hand edit.

**Decided (2026-10-13): brewing decks are the exception.** While a deck is
marked brewing, edits change its **list** right away (so the brew shows on
the Board, with stats, like any deck) but move **no** copies. Each add's
chosen source is saved, and nothing leaves storage or another deck until
you press **"Build it"**, which makes all the moves at once (Phase 5). That
way, planning a new deck never pulls cards out of the decks you're
playing.

---

## The problem, in one table

The same intent leaves the collection in different states depending on
where you make the edit:

| Edit | Where it lives | What happens to the collection today |
|---|---|---|
| Apply a planned swap / add / cut | `writes.apply_change()` → `execute_swap()` / `_sleeve_here()` / `_unsleeve()` | Free copy moved into the deck (or a starter lot made); the cut copy goes back to Box or `dest_location` |
| Add bar (Board and Mainboard tabs) | `pages/4_Deck_Editor.py` `_add_card_to_deck()` | Starter lot only if **no lot of that exact printing exists**; a free copy in Box is never moved |
| Board "Add to deck" on an idea | `deck_board.apply_board_action()` `op == "add"` | Nothing |
| Board +1 / −1 / Remove / Move to Shortlist | `apply_board_action()` → `bulk_update_deck_cards()` / `remove_deck_card()` | Nothing — the removed copy stays "sleeved" in the deck |
| Mainboard table save | `bulk_update_deck_cards()` | Nothing |
| Workbench contention "drop from deck" | `pages/8_Workbench.py` → `remove_deck_card()` | Nothing |

And the add side has no record of intent at all: the allocator
(`queries.plan_availability()`) *derives* "available / in another deck /
not owned", but you can never *choose* "take it from Gisa" or "I'm buying
one" and have that remembered. Removals already have `dest_location`
(Phase 6 of the Workbench rework); additions have no counterpart.

### Elsewhere in the app (second review, 2026-10-13)

A sweep of every write path outside the deck edits above found the same
kind of gap: a change lands in one place and isn't picked up, or isn't
recorded, somewhere else.

| Area | What's out of sync today | Fixed in |
|---|---|---|
| `writes.rename_deck()` | Updates `collection.location` but **not** `deck_changes.dest_location`, which also stores deck names. Rename a dismantle's target deck between staging and applying, and the apply moves the copies to a deck name that no longer exists | 0 |
| `writes.delete_deck()` | Deletes this deck's changes, but **other** decks' open rows with `dest_location` = this deck stay, and applying them sends copies to a phantom location | 0 |
| Mainboard quantity changes | A bump (Swamp ×4 → ×5) writes no history row by design (writes.py comment above `_put_deck_card`). Once Phase 1 makes +1 move a physical copy, the copy would move with no record | 0 |
| Every write's follow-up | Each page invalidates caches, reports and reruns by hand, and they don't match. A deck edit clears deck caches but not `load_owned_card_quantities` or the Collection page's caches; feedback is a mix of `st.toast`, `st.success`, `st.info` and stashed `RESULT_KEY` messages | 0 |
| Spellbook combos | `loaders.invalidate_deck_caches()`'s comment says the Decks page warns when a deck "has been edited since" the combo check. It doesn't; it only shows the timestamp and "Re-check after changing the decklist" | 0 |
| `loaders.load_cards_by_name` | Never cleared by any invalidator, so Land Probability's commander cards stay stale after a Card Data refresh | 0 |
| Game log after a rename | `game_participants.deck_name` keeps the old text, and the games list shows it, so a renamed deck's past games show under the old name while its win rate (by `deck_id`) follows the new one | 0 |
| "✅ Mark deck as built" (Decks page) | Clears the brewing flag without moving anything. Once brewing edits defer their moves, this would leave every listed card unpulled with nothing to say so | 1, 5 |
| Dismantled decks | No rule for edits to a dismantled deck's list (its cards are already gone). Phase 1 would start moving copies into it | 1 |
| Workbench Reconcile "Sleeved here" / "Put away", Dismantle "move strays" | Call `move_lot()` directly, with their own messages and no shared reporting | 1 (pop-up), 6 (log) |
| Collection page edit mode | Moving a lot into or out of a deck's Location, or deleting a sleeved lot, never touches that deck's list and leaves no record. Reconcile picks it up later as "to pull" / "to put away" | 6 |
| Order import / Collection Editor add | Importing straight into a deck's Location sleeves the copies but doesn't put the card on that deck's list | 6 |
| `prune_collection()` | Deletes lots silently as part of the Card Data refresh (it returns what it removed, but nothing keeps it) | 6 |
| Advisor | Verified suggestions have nowhere to go; you retype them into the Shortlist | 5 |

## Rules every edit follows

Four rules, so a later feature can be checked against them instead of
re-deriving them:

1. **Every write ends in `card_view.finish_edit(result)`** (or
   `stash_edit(result)` from inside a widget callback). One helper
   clears the right caches (deck caches for any deck write; collection
   caches too whenever the result carries a move or touches a lot;
   reference / game-log / combo caches by flag), stashes the message and
   moves for the pop-up, and reruns the app. No page calls `invalidate_*`,
   `st.toast` and `st.rerun` by hand after a write. Read-only notices
   (`st.info` hints, empty states) are unaffected.
2. **What is tracked, and where:**
   - which cards are on a deck, and how many → the deck's history
     (`deck_changes` applied rows), **including quantity changes** (Phase 0)
   - where each physical copy is → the move log (Phase 6), shown on Card
     Lookup
   - settings (tags, themes, deck info, build state, reference lists) →
     current state only, no history. This is deliberate: they describe the
     deck, they don't move cards. Add history for one only if it's asked
     for.
3. **Deck names stored as text cascade together.** `collection.location`,
   `deck_changes.dest_location`, `deck_changes.source_location` (Phase 2)
   all hold deck names as text, matching the existing Location
   convention. One helper, `writes._cascade_deck_name(conn, old, new)`,
   updates all of them on a rename, and `delete_deck()` handles all of them
   on a delete. A new text column holding a deck name gets added to that
   helper in the same change.
4. **The deck's build state decides whether edits move copies.** A normal
   deck moves them right away. A brewing or dismantled deck changes its list
   only. That check lives inside the two move helpers (Phase 1), and
   `force=True` is the only way past it.

## The core idea: every deck edit produces a list of moves

A **move** is one physical step: `{card, quantity, collection_id, from,
to}`, where `from`/`to` is a storage location, a deck name, `None`
("new copy record"), or `"bulk"` (untracked basics, never moved). Every
deck edit, wherever it starts, goes through one pair of helpers that
decide and perform the moves, and every caller gets the same list back to
show in the pop-up and to keep for Undo.

---

## Phase 0 — Fix what's already out of sync

No schema change, and no change to what an edit does to the collection.
These are bugs or gaps in the app as it is, and Phase 1 builds on the
helper this phase adds.

### `card_view.finish_edit(result)` (rule 1)

`result` is `{"message": str, "moves": [...], "collection": bool,
"errors": [...]}`; `moves` stays empty until Phase 1 fills it. It runs
`loaders.invalidate_deck_caches()`, adds `invalidate_collection_caches()`
when `collection` is true or `moves` is non-empty, stashes the message
in one session key, and calls `st.rerun(scope="app")`. A small
`card_view.show_pending_feedback()`, called once from `setup_page()`,
shows it on the next run. That one key replaces the per-page
`RESULT_KEY` / `result_key` / `combo_flash` stashes.

Adopt it on every page that writes: Deck Editor, Decks (build state,
combos), Workbench, Collection, Collection Editor, Card Database, Game
Tracking, and `deck_board.run_board_action()`. Pages that only read are
untouched. Phase 1's `show_moves()` becomes the part of
`show_pending_feedback()` that renders moves (toast for one or two, dialog
for more).

### Deck names (rule 3)

- `writes._cascade_deck_name(conn, old, new)`: updates
  `collection.location` (what `rename_deck()` already does, behind
  `update_collection_locations`), plus `deck_changes.dest_location` on
  **open** rows. Applied rows keep the name they were applied under,
  because history records what happened.
- `delete_deck()`: other decks' open rows whose `dest_location` is the
  deleted deck get `dest_location = DEFAULT_LOCATION`, the same fallback
  its lots already get. The page says how many were redirected.
- Game log: the games list shows the **current** deck name for rows linked
  by `deck_id` and keeps the recorded text for untracked decks.
  `game_participants.deck_name` is not rewritten, so the recorded text
  survives a later delete (which detaches the row).

### Quantity changes in history (rule 2)

`bulk_update_deck_cards()` logs a quantity change as an applied row too:
an increase as `add_scryfall_id` with `quantity` = the increase, a
decrease as `remove_scryfall_id` with `quantity` = the decrease, and
`notes = "quantity 4 → 5"`. Join/removal logic is unchanged:
`_auto_drop_stale_ideas()` still fires only on a genuine join. Check every
reader of history first (`load_deck_change_history`, the Decks History
tab, Card Lookup, the Advisor context) so nothing counts a bump as a new
card. Filter on the note prefix where a reader needs joins only.

### Smaller fixes

- **Combo staleness:** the Decks page compares the deck's latest applied
  change (`resolved_at`) with `deck_combo_sync.fetched_at` and shows a
  warning when the list changed after the check, as the loader comment
  already claims it does.
- **`load_cards_by_name`** is cleared by `invalidate_reference_caches()`
  and `invalidate_deck_caches()`.

### Tests

`_cascade_deck_name()` updates `collection` and open `dest_location` and
leaves applied rows; delete redirects other decks' open rows to Box; the
games list shows the renamed deck's current name; quantity changes log
one applied row each, and readers that count joins don't count them; the
staleness warning appears after an edit and clears after a re-check;
smoke: every page that writes still writes, with the message shown after
the rerun.

### Done when

Renaming or deleting a deck leaves nothing pointing at the old name;
every write on every page finishes the same way; every change to a deck's
list, quantities included, is in its history.

### As built (2026-10-13)

Where the build differs from, or adds to, the spec above:

- **`finish_edit(result)` takes more than the four keys above.** Writes
  outside deck/collection data needed their own caches cleared, so the
  result also accepts `reference`, `games` and `combos` flags (each clears
  that `loaders.invalidate_*` group), plus `warnings` (⚠️ toasts for
  non-fatal notes such as "kept as typed", or skipped dismantle cards).
  `message` may be a list: one or two lines are toasts, three or more are
  one `st.success` list at the top of the page, so a batch apply isn't cut
  off in a toast. With these, no page calls an invalidator by hand.
- **`result_from_lines(lines, **flags)`** turns the "✅ …" / "❌ …" line
  lists the Shortlist tab and the Workbench already built into a result.
  Both pages keep a small local `_refresh()` because they also bump the
  version counter their data_editors are keyed on.
- **`stash_edit(result)`** is `finish_edit()` minus the rerun, for writes
  made inside a widget callback, where `st.rerun()` does nothing.
  `deck_board.run_board_action()` uses it; its existing dirty flag and
  `rerun_if_edited()` already do a full-app rerun (`BOARD_RERUN_SCOPE` is
  `"app"`), and `setup_page()` shows the message on that run. A refused or
  no-op Board edit writes nothing, so it still toasts directly.
- **`delete_deck()` returns `(name, error, redirected)`**: every caller
  was updated, including the two `scripts/_test_*_offline.py` that
  unpack it.
- **No reader needed the note-prefix filter.** The only readers of applied
  history are the Decks page's History tab and Card Lookup's history list.
  Both list rows for display, and a quantity change belongs on both. The
  Advisor doesn't read `deck_changes` history. `_auto_drop_stale_ideas()`
  still runs on a genuine join only, and there's a test for that.
- **Rename cascade and the collection checkbox:** the Deck Editor's "Also
  update matching collection Locations" box still gates
  `collection.location` only. Open `dest_location` rows always follow the
  rename, since they're plan data, not collection data.
- **Combo staleness** is a `stale` key on `queries.deck_combo_sync()`'s
  result (latest applied `resolved_at` later than `fetched_at`), so the
  loader comment that promised it is now true.
- **Not routed through `finish_edit()`, on purpose:** dialog Close/Cancel
  buttons, the Advisor's chat-history reruns, and Card Database's
  "Export lists to CSV" (writes files, not the database).

## Phase 1 — One write path, moves shown

No schema change. Behavior change: direct edits now move copies.

### Writes (`dashboard_lib/writes.py`)

- **`sleeve_into_deck(conn, deck_id, scryfall_id, quantity, source=None,
  force=False)` → [move, ...]** — puts `quantity` copies into the deck's
  sleeves. Built from `_sleeve_here()`, made quantity-aware. With
  `source=None` ("Auto") the rule is the one `execute_swap()` already
  uses: copies already sleeved here count first; then free copies in
  storage (biggest lot first, splitting with `move_lot()`, the same order
  `plan_availability()` allocates in); then a starter lot for the
  remainder (source `"Auto-added (deck edit)"`). **Never** takes a copy
  out of another deck silently — that becomes an explicit choice in
  Phase 3.
- **`unsleeve_from_deck(conn, deck_id, scryfall_id, quantity, dest=None,
  force=False)` → [move, ...]** — built from `_unsleeve()`. Moves up to
  `quantity` copies sleeved in the deck to `dest` (default
  `DEFAULT_LOCATION`, "Box"). Matched by oracle_id with the name fallback,
  like the rest of the change code.
- **`force`** exists only for Phase 5's "Build it" (every other caller
  leaves it `False`). It is the one way past the brewing check below — see
  that paragraph for why a plain build_state check can't tell "an edit on
  a brewing deck" apart from "Build it moving a brewing deck's cards".
- **`add_deck_card()` / `remove_deck_card()` / `bulk_update_deck_cards()`**
  call these and **return the moves**. A quantity change moves only the
  difference (+1 sleeves one more copy, −1 puts one away). The applied
  history row they already log is unchanged.
- **`execute_swap()` and `apply_change()`'s pure add / pure cut** switch to
  the same two helpers and return moves too (keeping their current
  `moved_out` / `assigned_lot_id` / `created_lot` keys alongside, so
  existing callers don't break). This also fixes `execute_swap()` moving
  *every* lot of the cut card by name regardless of the swap's quantity.
- **`auto_add_to_collection()`** is no longer called by the Deck Editor
  (the Auto rule above replaces it). Keep the function — tests and the
  history notes refer to it.

Basic lands with an untracked (NULL) quantity are never moved; their move
reads "bulk — not tracked".

**Brewing and dismantled decks** (`decks.build_state` is `'brewing'` or
`'dismantled'`, and `force` is not set; rule 4). A dismantled deck's list
is a record of what it was, and its cards are already elsewhere, so
editing that record mustn't pull copies back in. Its placeholder reads
*"Dismantled — list only, nothing moved."* For both states,
`sleeve_into_deck()` and `unsleeve_from_deck()` touch no lot and
return one placeholder move, `{"from": "brewing", "to": "brewing"}` rather
than an empty list — so `show_moves()` needs no separate code path, it
just renders as *"Brewing — Sol Ring will be pulled when you build the
deck."* like any other move, and `apply_board_action()`'s Undo plumbing
(which expects a move list) doesn't need a special case either. One check
inside the two helpers, not at each caller, so no entry point can skip it;
`force=True` (Phase 5's "Build it") bypasses it and moves the lot for
real — that's the only way "Build it" can ever do its job on a brewing
deck, since the deck is still marked brewing for the whole batch (a
partial build leaves it that way on purpose; see Phase 5). Until Phase 5
ships, the existing Build Plan / Reconcile "to pull" flow covers getting
the cards in, as it does today.

### Callers routed through it

Every row of the first table above ("The problem"): the add bar, every Board op (`inc`, `dec`,
`remove`, `to_shortlist`, `add`, `set_qty`, `from_shortlist` and their
Undo), the Mainboard table save, the Workbench contention drop, and
`apply_changes()` (Shortlist tab, Board rail, Workbench).

**Lot-only moves** don't change a list, but they report the same way:
Workbench Reconcile's "Sleeved here" and "Put away", and Dismantle's
`move_dismantle_strays()`. They keep calling `move_lot()`, now through a
small `writes.move_copies(conn, collection_id, location, quantity)` that
returns the same move dicts, so their results go through `finish_edit()`
and the pop-up (and, in Phase 6, the move log) like everything else.

**"✅ Mark deck as built"** (Decks page Build Plan). Until Phase 5
replaces it with "Build it", it counts the brew's cards not yet sleeved
and says so before clearing the flag (*"12 cards are still to pull.
They'll stay on Reconcile until you sleeve them."*). That way, marking a
deck built never quietly leaves its cards unpulled.

### The pop-up

One shared function, `card_view.show_moves(moves)`, called by Phase 0's
`show_pending_feedback()` (never by a page directly):

- one or two moves → `st.toast`, e.g. *"Sol Ring: Box → Prosper"* /
  *"Arcane Signet: new copy record → Prosper"*.
- more than that (a Mainboard table save, an apply batch) → an
  `st.dialog` listing every move, grouped by from → to, so a batch is
  never summarised as just a count.

Moves reach it through `finish_edit(result)` (Phase 0), which stashes
them in the one feedback key before the rerun, so a full-app rerun
doesn't eat the message. Because any non-empty `moves` also clears the
collection caches, the Collection page, Home summary and Board ownership
badges are never stale after a deck edit.

### Undo

The Board's one-level Undo payload carries the moves. Undoing an edit
reverses the deck change **and** moves each lot back to its `from`
(`writes.revert_moves(conn, moves)`; a "new copy record" move is undone by
deleting that lot, only if it's untouched since). A lot that `move_lot()`
split stays as two lots after the undo — same printing, same location,
harmless; note it rather than add merge logic. Brewing placeholder moves
are skipped — there's no lot to put back.

This is a deliberate function, not just "call the inverse write again."
In Phase 1, with only the Auto rule, re-running the inverse op (e.g.
`add_deck_card` to undo a `remove`) happens to land the lot back in the
same place Auto would pick anyway, so the two approaches look identical
here. They stop being identical in Phase 2: once an edit can name an
exact storage location, Undo has to put the lot back in *that* location,
not wherever a fresh Auto run would choose — which only `revert_moves()`
working off the recorded move list can guarantee. Building it this way
now avoids a silent behavior change when Phase 2 lands.

### Tests

- New `tests/test_edit_flow.py`: each entry point (add bar helper, every
  Board op, `bulk_update_deck_cards`, `apply_change` all three shapes)
  leaves `collection` in the same state for the same intent — a table of
  cases run through every path. Free copy in Box is used before a starter
  lot is made; a copy in another deck is never taken; −1 on a 3-copy lot
  splits it; bulk basics don't move; Undo restores the lot locations; on a
  brewing or dismantled deck every path changes the list and moves
  nothing; Reconcile / strays moves report through `finish_edit()`; "Mark
  deck as built" shows the to-pull count.
- Update `tests/test_deck_board.py` and `tests/test_deck_changes_writes.py`
  expectations where removals now move copies.
- Smoke (`tests/test_pages_smoke.py`): Deck Editor add bar and Mainboard
  save show the pop-up.
- On a real DB copy: Reconcile's "to pull" / "to put away" counts before
  and after a scripted round of edits, noted here.

### Done when

Adding, bumping, removing or swapping a card from any page leaves the
collection in the same state, a pop-up names every copy that moved, and
Undo on the Board puts the copies back.

---

## Phase 2 — Choose where a card comes from, and where it goes

### Schema (additive, via `queries._SCHEMA_UPGRADES`)

- `deck_changes.source TEXT` — NULL = Auto (Phase 1's rule), `'storage'`,
  later `'deck'` (Phase 3) and `'buy'` (Phase 4).
- `deck_changes.source_location TEXT` — the storage location (or, in
  Phase 3, the deck) the copy comes from.

Both live on `deck_changes` so they apply to staged ideas **and** to the
applied history rows direct edits already write — History can then say
*"added from Lands Box"*. NULL everywhere keeps every existing row reading
as it does now.

`source_location` holds deck names as text from Phase 3 on (and storage
locations, which a deck rename doesn't affect), so it joins
`_cascade_deck_name()` (rule 3) in this phase. A storage location renamed
or removed in `location_catalog` changes nothing here, the same as it
changes no lot's Location today.

### Reads (`dashboard_lib/queries.py`)

- **`card_sources(conn, scryfall_id, deck_id)`** → the options for one
  card: each storage location holding free copies (with quantity), each
  other deck holding one (with quantity — shown from Phase 3), the price
  (for Buy, Phase 4), and which option Auto would pick. One query, so the
  picker and the Board payload agree.
- **`plan_availability()`** honors a chosen source: `storage` +
  `source_location` claims only from that location (verdict text says so,
  and it's `claimed_by` / short if that location runs out).

### UI

- **One source picker**, `card_view.render_source_picker(...)`, built from
  `card_sources()`, defaulting to Auto. Used by the add bar and the
  Shortlist tab's "Add an idea" form, and as a column in the Shortlist
  ideas table.
- **Board (JS):** each Shortlist card's payload gets its source options;
  "Add to deck" and "Apply" open a small source menu (Auto first). The
  trigger gains `"source"` / `"source_location"`. The card menu's
  **Remove** and **−1** gain a "to…" submenu (Box first, then the other
  storage locations); the choice rides on the trigger as `"dest"`.
- **Mainboard table:** one "Removed copies go to" selectbox beside Save
  (default Box) rather than a column per row.

### Tests

`card_sources()` options and Auto pick; a chosen storage location is used
even when Box has a bigger lot; `plan_availability()` with a pinned
location that runs short; Board triggers carry source/dest through
`apply_board_action()`; history rows store the source.

**Brewing decks:** the chosen source is saved on the add's history row as
usual, but no copy moves (Phase 1's rule). `plan_availability()` counts a
brewing deck's not-yet-pulled cards as claims on their chosen source, so a
swap planned elsewhere can't promise the same copy.

### Done when

Every add lets you pick the storage location (or leave it on Auto), every
removal lets you pick where the copy goes, and History shows both.

---

## Phase 3 — Take a card from another deck

### Schema

- `deck_changes.linked_change_id INTEGER` — ties a donor deck's removal to
  the receiving deck's add. Today `stage_dismantle()` pairs rows only by
  change_id order and a note; this makes the pair explicit.

### Behavior

- **Direct add with source "from Gisa":** this edits another deck, so it
  asks first (*"This also takes Sol Ring out of Gisa's list. Continue?"*).
  On confirm, one new write, **`writes.transfer_between_decks(conn,
  from_deck_id, to_deck_id, scryfall_id, quantity)`**, removes it from
  Gisa's list and adds it here, moving the lot Gisa → this deck with no
  stop in the box (`unsleeve_from_deck(dest=<this deck>)` then
  `sleeve_into_deck()`, which finds the copy already sleeved here). Both
  decks get a linked applied history row with `source='deck'`,
  `source_location='Gisa'`. The pop-up names both decks.
- **Staged (a Shortlist idea or planned swap from Gisa):** writes a linked
  `planned` removal on Gisa with `dest_location` = this deck — the same
  shape a dismantle transfer already uses. Applying either side applies
  both, removal first. Dropping or unpairing one side drops the other,
  with a message naming it.
- **Brewing deck taking from Gisa:** the brew's list gains the card right
  away, but Gisa's side is written as a linked **planned** removal, not
  applied. Gisa's list and sleeves stay as they are, so no confirm is
  needed, and Gisa's Board shows the card as "promised → <brew>". If you
  drop the card from the brew first, the planned removal on Gisa is
  dropped with it.

  Note this pair is **lopsided**: the brew's add is already `applied` (the
  list changed), only Gisa's removal is `planned`. The "applying either
  side applies both" rule above assumes both sides are planned, so it
  must not try to re-apply the brew's add. The pair rules are therefore:
  apply/drop/unpair act on whichever linked rows are still open, and skip
  any that are already applied. "Build it" (Phase 5) is what applies
  Gisa's leftover removal.
- **Writing the pair:** `add_change()` can't do it as it stands. It
  rejects a `planned` row with no card to cut ("A planned swap needs a
  card to replace"), which is exactly the receiving deck's half of a
  staged take, and it only guards against duplicate *adds*, not a second
  cut of the same card on the donor. Add one writer,
  **`writes.add_linked_take(conn, to_deck_id, from_deck_id, scryfall_id,
  quantity, status)`**, which writes both rows the way `stage_dismantle()`
  does (removal first, so change_id order applies the cut before the add)
  and runs the removal-side guard `_open_removal_for_card()` on the donor,
  so two decks can't both queue a take of Gisa's one copy. The allocator
  shows the second deck's attempt as `claimed_by` before it gets that
  far.
- `stage_dismantle()` sets `linked_change_id` on the pairs it writes from
  now on, **and** `source='deck'` / `source_location=<source deck>` on the
  add rows, so a dismantle transfer reads "from Gisa" everywhere, the same
  as one you chose by hand. Old pairs are not backfilled.
- **Deleting a deck in a pair:** `delete_deck()` drops the other deck's
  open linked rows (a planned take from a deck that no longer exists can't
  be applied), says how many, and clears `linked_change_id` on applied
  rows that pointed at the deleted deck's history, so no reference
  dangles.

### Where it shows (this is the "flow everywhere" part)

- **Donor deck's Board:** the card gets a "leaving → Prosper" badge
  (like `cut_for` today), and its Shortlist rail lists the linked removal.
- **Receiving deck's Board:** the Shortlist card reads "from Gisa".
- **Workbench Planned changes:** a linked pair is one row,
  *"Sol Ring: Gisa → Prosper"*, applied or dropped together, and counts
  as one in the "Planned changes (N)" header — today each row counts on
  its own, which would double-count every pair.
- **Card Lookup:** pending moves for the card.
- **Allocator:** a linked pair claims Gisa's copy, so any other plan
  wanting that copy now shows `claimed_by`.

### Tests

Direct and staged from-deck adds leave both decks' lists, the lot and both
histories right; apply / drop / unpair either side handles the pair,
including a lopsided brewing pair (only the open side is touched);
`stage_dismantle()` links its pairs and sets their source; the allocator
sees the claim; the Workbench shows and counts one row per pair.

### Done when

You can take a card from another deck from the add bar, the Shortlist or
the Board, and both decks show it — before (staged) and after (history).

---

## Phase 4 — Planned buys

### Behavior

- **Source "Buy"** on any add. A direct add puts the card on the deck's
  list with **no** collection lot (no phantom copy), and history stores
  `source='buy'`. A staged idea with Buy stays on the Shortlist as usual.
- Buy is allowed even when you own copies elsewhere — "keep Gisa's, buy a
  second" is the case the current buy list can't express.

### Buy list (`queries.buy_list()`, Workbench)

Two new reasons alongside the two it has today, each row naming its decks:

- `idea` — open idea you own zero copies of (as now)
- `contention` — a mainboard/shortlist shortfall from `contention_table()`
  (as now)
- `chosen` *(new)* — open idea or planned change with `source='buy'`
- `awaiting` *(new)* — on a deck's list from a Buy add, still no copy
  sleeved there

A card can qualify under more than one (an idea you own none of *and*
marked Buy); it gets one row, under the most specific reason
(`awaiting` > `chosen` > `contention` > `idea`).

Each row gets **"Bought"**: enter price paid (optional), and it creates
the lot sleeved straight into that deck (shown in the pop-up).

### Order import (`dashboard_lib/order_import.py`, Collection Editor)

After an import, match the new lots by oracle_id against `chosen` /
`awaiting` buys and offer "Sleeve into Prosper" per match (ticked by
default). Unmatched lots import as they do now.

### Board

A "buying" badge, distinct from today's "not owned" (owned zero, no
decision made).

### Tests

Buy add creates no lot; the buy list's four reasons, and a card that
qualifies under two lands once under the more specific one; "Bought" sleeves the lot into the right deck;
order import matching (one match, two decks wanting it, no match).

### Done when

You can add a card you mean to buy, see it on the buy list, and record
the purchase — by hand or by importing the order — and have it land in
the right deck.

---

## Phase 5 — Building new decks, and one place per job

### Building a deck

- The Decks page Build Plan reads the chosen sources: **Pull from storage**
  (grouped by location), **Take from other decks** (linked pairs), **Buy**,
  plus the existing pull sheet with the same grouping.
- A **"Build it"** button (Build Plan, and on the Board while brewing)
  makes every move the brew has been waiting on, in one batch, through
  **`writes.build_deck(conn, deck_id, oracle_ids=None, dry_run=False)`**:
  each lopsided linked removal on a donor deck (Phase 3) is applied first
  through the normal `apply_change()` (the donor isn't brewing, so no
  `force` is needed; its copy moves donor → brew), then each listed card
  still not sleeved in the brew is pulled from its saved source with
  `sleeve_into_deck(..., force=True)` (storage location or Auto). Donor
  first, so the brew's sleeve step finds the transferred copy already
  there instead of making a starter lot for it. Buy cards stay on the buy
  list. The dialog shows the full move list from `dry_run=True` as a
  **preview** before anything is written, then you confirm.
- **Clearing the brewing flag:** only once nothing is left to pull except
  Buy cards. A partial build leaves the deck brewing.
- Anything that can't be pulled (a copy since taken by another plan, a
  donor card no longer in that deck) is listed and left as "to pull", not
  silently skipped.
- **Partial builds:** `build_deck(oracle_ids=...)` takes a selection, the
  same way `stage_dismantle()` takes `transfer_oracle_ids`. For the UI, use
  one **"Pull this group"** button per source group (each storage
  location, each donor deck) rather than per-row `data_editor`
  checkboxes: `PROJECT_STATE.md` already notes the per-row checkbox
  version of exactly this was skipped because `AppTest` can't drive
  `data_editor` checkbox state. Buttons are testable; the selection
  parameter is tested directly either way.

### One place per job

- **Board** — where you edit one deck (adds, sources, swaps, removals).
- **Workbench** — the cross-deck view: conflicts, linked pairs, buys,
  reconcile.
- **Shortlist tab** — trimmed to the bulk table (notes, review flags,
  quantities, sources) and History; its add form uses the shared picker.
- **Mainboard tab** — kept for bulk quantity edits only, through the
  Phase 1 path. Decide whether to retire it after Phases 1–4 get real use.
- **Decks page Cards tab, Card Lookup** — view only, now showing source /
  destination ("from Gisa", "buying", "leaving → Prosper").
- **Decks page Build Plan** — "✅ Mark deck as built" is replaced by
  "Build it". Marking a deck built without pulling stays available only as
  a secondary choice in the Build it dialog ("I'll sleeve them myself"),
  with the Phase 1 to-pull count.
- **Advisor** — a verified suggestion gets an **"Add to Shortlist"**
  button, written through `add_change()` as an idea with
  `notes = "Advisor: <skill>"` (a suggested cut fills in "Replaces"). The
  Advisor still never touches a deck's list or the collection. Its header
  docstring changes from "read-only throughout" to "adds Shortlist ideas
  only".

Update the page header docstrings for every page whose layout changes.

### Tests

`build_deck()`: the dry run writes nothing and lists the same moves the
real run makes; storage, Auto and donor-deck sources each land in the
brew; the donor removal is applied before the brew's sleeve step (no
starter lot is made for a transferred card); a partial build by
`oracle_ids` leaves the deck brewing; a full build clears the flag even
with Buy cards outstanding; a copy taken by another plan since it was
listed is reported as "to pull", not skipped. Smoke: Build Plan renders
the grouped sources and the per-group buttons for a brewing deck.

### Done when

A new deck can go from empty to built with every card's source chosen,
the Build Plan reflects those choices, and each kind of edit has one home.

---

## Phase 6 — Collection-side edits, tracked and flowing back

Phases 1–5 make deck edits update the collection. This phase handles the
other direction, edits that start on the collection side, and gives every
copy a history (rule 2). It depends only on Phase 1's move dicts, so it
can follow Phase 1 directly.

### Schema (additive)

A new table, `collection_moves`:

| column | |
|---|---|
| `move_id` | INTEGER PK |
| `collection_id` | the lot (no FK, so a deleted lot's history survives) |
| `scryfall_id`, `card_name` | copied at write time, for the same reason |
| `quantity` | |
| `from_location`, `to_location` | text, NULL = "new copy record" / "deleted" |
| `reason` | `deck_edit`, `apply`, `reconcile`, `dismantle`, `collection_edit`, `import`, `prune`, `deck_delete` |
| `change_id` | the `deck_changes` row that caused it, when there is one |
| `moved_at` | TIMESTAMP |

Rule 3 applies: `from_location` / `to_location` are history, so a rename
leaves them alone, the same as applied `deck_changes` rows.

### One choke point

Every lot write already goes through `add_collection_lot()`,
`update_collection_lot()`, `move_lot()`, `remove_collection_lot()`,
`bulk_update_collection()` or `prune_collection()`, plus the two bulk
`UPDATE collection SET location` statements in `rename_deck()` /
`delete_deck()`. Each one writes its move rows. `rename_deck()` writes
none, since a rename relabels and nothing physically moves. A
`delete_deck()` reassignment writes `deck_delete` rows. The Phase 1 move
helpers pass their `change_id` through, so Card Lookup can link a move to
the deck change that caused it.

### Collection page edits that touch a deck

When the Collection page's edit-mode save moves a lot into a deck's
Location, out of one, or deletes or shrinks a lot sleeved in a deck,
`finish_edit()` opens a dialog listing those lots by deck before the
save is final:

- **"Also update Gisa's list"**: adds or removes the card on that deck
  through Phase 1's `add_deck_card()` / `remove_deck_card()`, with the
  lot move already done, so nothing moves twice. History and the move log
  both record it.
- **"Just the collection"**: the save stands as a records correction
  (`reason='collection_edit'`), and the deck shows it on Reconcile as it
  does today.

There's no silent default either way (open question 4). A save that
touches no deck shows no dialog.

### Order import / Collection Editor add

Importing or adding lots straight into a deck's Location offers the same
"also add to <deck>'s list" choice per deck. This sits alongside Phase 4's
buy matching: a lot that matches an open buy is sleeved by that path and
isn't asked about twice.

### Prune

`prune_collection()` writes a `prune` move row for each lot it deletes,
and the Card Data refresh summary lists them. They also show on Card
Lookup.

### Card Lookup

A **History** section per card: the move log (where each copy went,
when, why) next to its `deck_changes` history, so *"where did my Blood
Crypt go?"* has one answer.

### Tests

Each lot write logs exactly the moves it made; a rename logs none; a
delete logs `deck_delete`; Phase 1 deck edits log with their `change_id`;
Collection save into or out of a deck shows the dialog, and each choice
leaves the list, the lot and both histories right; order import into a
deck location offers the list add, and skips it for a buy match; prune
logs its deletions; Card Lookup shows both histories.

### Done when

Every physical move of a copy, whichever page made it, is in the move
log. An edit on the collection side that affects a deck asks whether the
deck's list should follow.

---

## Open questions

1. ~~Brewing decks: stage or move right away?~~ Decided 2026-10-13: the
   list changes right away, copies wait for "Build it" (see the top).
2. **Proxy as a source.** `PROJECT_STATE.md` has a long-open item that the
   real decision is often "buy it, or proxy it". The Phase 2 picker is the
   natural place for a `'proxy'` source (no lot, not on the buy list).
   Not planned until the Location/Proxy convention is decided.
3. **Auto when copies exist only in other decks.** Phase 1 keeps today's
   rule (make a starter lot rather than take another deck's copy), which
   can record a copy you don't own. From Phase 2 the picker shows the
   other decks; consider making Auto *ask* in that case instead of
   creating a lot.
4. **Collection edits that touch a deck: ask, or pick a default?** Phase 6
   asks every time. If the dialog gets tiresome in practice (say, during a
   big records cleanup), add a per-save "don't ask, just the collection"
   tick rather than changing the default.
5. **Settings history.** Rule 2 keeps tags, themes, deck info and build
   state as current state only. If you want, for example, "when did this
   deck go from brewing to built" on the History tab, that needs a small
   deck-event table. `deck_changes` can't hold it, because its CHECK
   requires a card.

## Out of scope

- Drag-and-drop on the Board (VISUAL_DECK_PLAN C2).
- Multi-level undo.

## Conventions to hold to

Carried forward from [VISUAL_DECK_PLAN.md](VISUAL_DECK_PLAN.md):

- **`queries.py` reads, `writes.py` writes, `loaders.py` caches, pages
  render.** `deck_board.py` stays UI glue with no SQL. Schema changes are
  additive only, via `_SCHEMA_UPGRADES`, and mirrored in `schema.sql`.
- Write functions raise `ValueError` with a user-facing reason, shown as a
  toast — never a silent no-op.
- **Module header comments + inline comments for non-obvious logic**, in
  the JS too.
- **Tests in `tests/`** against the in-memory `conftest.py` fixture; every
  touched page smoke-tested with `AppTest`; Board JS checked in headless
  Edge on a DB copy (`dashboard_lib/components/README.md`).
- **Run with the `mtg1` conda env.**
- Update `PROJECT_STATE.md` and `README.md` at the end of each phase, and
  tick the checklist above with a one-line "done" note.
