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

The `add_card` fixture gives every card an `oracle_id` derived from its
name, so printings sharing a name group together the way real Scryfall data
does. Pass `oracle_id=` explicitly where name and oracle identity need to
come apart, or `oracle_id=False` for a card with none at all.

The older `scripts/_test_*_offline.py` files are separate standalone
scripts (run them directly, with `PYTHONIOENCODING=utf-8` on Windows); they
are not collected by pytest.
