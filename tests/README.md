# tests/

pytest suite that runs against a throwaway in-memory SQLite database built
from `schema.sql` (see `conftest.py`) — it never touches `mtg_collection.db`.

Run from the project root, in an environment with the dashboard's packages
plus pytest (`pip install -r requirements-dev.txt`):

    python -m pytest tests

- `conftest.py` — `conn` (fresh empty DB) and `add_card` (insert a minimal
  card row) fixtures.
- `test_collection_writes.py` — add / bulk-update / delete / prune of
  collection lots (the code behind the Collection page's Edit mode).
- `test_deck_changes.py` — the `deck_changes` table's creation on an older
  database and the backfill from the superseded `maybeboard` /
  `deck_swap_queue` tables (Workbench rework, Phase 0).
- `test_deck_changes_writes.py` — the idea -> planned -> applied/dropped
  lifecycle (Phase 1), the stale-idea auto-drop and conflict resolution
  (Phase 2), and the places that used to read the maybeboard.
- `test_plan_availability.py` — the plan allocator (Phase 2): allocating
  your actual copies across a whole queue of shortlist rows instead of
  answering each row in isolation, matching on `oracle_id`, and treating a
  NULL `collection.quantity` as one copy rather than none.
- `test_contention.py` — contention table, buy list and Card Lookup (Phase 3).
- `test_reconcile.py` — reconciliation (Phase 4): unlocated lots, the
  per-deck list-vs-physical diff in both directions, build status, and
  `move_lot()` splitting a lot when only some copies move.
- `test_deck_history.py` — change history, the deck comparison matrix and
  tile status strips (Phase 5): timeline ordering, the games-between-changes
  day rule, dropped rows not starting a period, and per-deck counts.
- `test_advisor.py` — advisor grounding layer (see
  `dashboard_lib/advisor/README.md`): role heuristics, candidate pool, guard
  validation and the retry loop, offline via `FakeProvider`. The `test_eval_*`
  cases are the starter eval set (fabricated name, misquote, wrong role...).
- `test_deck_lifecycle.py` — brewing and dismantling (Phase 6): the
  sourcing plan's buckets being mutually exclusive and summing to the
  non-basic count, donor-deck impact (including the leftover lot that costs
  its deck nothing), the dismantle split into direct transfer / box /
  strays, each row carrying its own deck's printing, staging writing
  nothing physical, a transfer's lot moving source -> target without a stop
  at "Box", and the pure-add / pure-remove apply paths.

- `test_pages_smoke.py` — `AppTest` smoke run of every page against a small
  temp database on disk (never the real one): each page through the
  `dashboard.py` router, and each page except Home on its own. Also checks
  the shared current deck (UI plan Phase 2): every deck page opens on it,
  it carries across pages, a `?deck_id=` param seeds it once, and creating a
  deck in the Deck Editor selects the new one.
- `test_finish_edit.py` — EDIT_FLOW_PLAN.md Phase 0: `card_view.result_from_lines()`, and through
  `AppTest` that a write's message shows exactly once after `finish_edit()`'s rerun (toasts for one or
  two lines, one success list for more, warnings and errors alongside). Page adoption is covered by
  `test_pages_smoke.py` (rename, Shortlist add, game delete, Workbench contention drop, Location catalog).
- `test_edit_flow.py` — EDIT_FLOW_PLAN.md Phase 1: one scenario run through every entry point that can
  express it (add bar write, each Board op, the Mainboard table save, `apply_change()`'s three shapes),
  each on a fresh database, asserting they leave the same list and collection. Then the rules one at a
  time: a Box copy before a starter lot, never another deck's copy, splitting a lot, −1 on an
  under-sleeved deck, printing swaps, bulk basics, Undo putting each copy back (and deleting a starter
  lot), brewing / dismantled decks, and lot-only `move_copies()`. The page side (add bar toast, Mainboard
  save dialog, "Mark deck as built" count, Reconcile "Sleeved here", Board Undo) is in `test_pages_smoke.py`.
- `test_collection_moves.py` — EDIT_FLOW_PLAN.md Phase 6: each lot writer logs exactly the moves it made
  (new copy record, move, split, quantity up/down, delete, bulk, prune, a deck's deletion), a rename logs
  none, Phase 1's deck edits log with their `change_id`, and `queries.deck_sync_offers()` /
  `writes.sync_deck_lists()` offer and apply list changes without moving a lot twice. The page side (a
  Collection save asks about the list, the dialog's two answers, Card Lookup's "Where copies went") is in
  `test_pages_smoke.py`. A click inside a real `st.dialog` reruns only the dialog, which `AppTest` can't
  do, so the answer buttons are driven through a script that opens the dialog on every run; the
  fragment-scoped rerun for a multi-deck dialog was checked in headless Edge against a scratch database.
- `test_current_deck.py` — pure tests of `card_view.seed_current_deck()` and
  `resolve_current_deck()` with a dict standing in for session state.
- `test_nav.py` — the page registry in `dashboard_lib/nav.py` matches the
  files in `pages/`, and the Decks page keeps its `Decks` URL.
- `test_deck_board.py` — VISUAL_DECK_PLAN.md Phase A: `board_payload()`
  (commander first, MDFC land flags, not-owned flags, URL-only images,
  payload size), plus Phase B's `apply_board_action()`: each op's effect on
  `deck_cards`, `deck_changes` history rows and tags, each returned undo
  restoring the prior state, and malformed / stale actions raising; and
  Phase C's Shortlist payload (verdicts across decks, name-only planned
  swaps, "cut for" marks) and plan / unpair / apply, with `apply` compared
  table-for-table against `writes.apply_changes()` (the Shortlist tab's
  path), plus drop / add / reopen (including typed-name ideas linked through
  the resolver). The JS itself is checked by hand; smoke tests in `test_pages_smoke.py` cover the Decks page (View = Stacks), the Deck Editor Board tab (add bar, Shortlist ideas / planned swap) and the edit/undo glue (incl. plan then apply).
- `test_static_images.py` — UI plan Phase 4: cached images become
  `app/static/` URLs on screen and data: URIs only when embedded (the deck
  sheet), the one-time `image_cache/` → `static/image_cache/` move
  (nested files, idempotent, never overwrites), and AppTest checks that the
  Collection grid carries no base64 and that both export expanders build
  only when open. Uses a temp `static/`, never the real one.

The `add_card` fixture gives every card an `oracle_id` derived from its
name, so printings sharing a name group together the way real Scryfall data
does. Pass `oracle_id=` explicitly where name and oracle identity need to
come apart, or `oracle_id=False` for a card with none at all.

The older `scripts/_test_*_offline.py` files are separate standalone
scripts (run them directly, with `PYTHONIOENCODING=utf-8` on Windows); they
are not collected by pytest.
