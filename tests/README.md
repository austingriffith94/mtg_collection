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

The older `scripts/_test_*_offline.py` files are separate standalone
scripts (run them directly, with `PYTHONIOENCODING=utf-8` on Windows); they
are not collected by pytest.
