"""AppTest smoke coverage for every page in pages/ (Phase 0 of
UI_ACTION_PLAN.md). Builds a small file-backed temp database (same
schema.sql + ensure_schema path as conftest's in-memory `conn` fixture,
but on disk since dashboard_lib.db.get_connection() opens a path, not an
existing connection) with just enough data — one deck, one commander,
one collection lot, one logged game — that every page has something to
render instead of only exercising its empty state. Never touches the
real mtg_collection.db; db.DB_PATH is monkeypatched to the temp file for
the duration of each test.

Two passes (Phase 1 added the second): every page through the real
dashboard.py st.navigation router, and every page except Home run on its
own, so pages stay runnable as standalone scripts. Home is router-only
(its st.page_link paths are relative to dashboard.py).
"""
import os
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES_DIR = os.path.join(PROJECT_ROOT, "pages")
PAGE_FILES = sorted(
    f for f in os.listdir(PAGES_DIR) if f.endswith(".py") and f[0].isdigit()
)
ROUTER_ONLY = {"0_Home.py"}


@pytest.fixture(scope="module")
def smoke_db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("smoke_db") / "mtg_collection.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(PROJECT_ROOT, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())

    conn.execute(
        "INSERT INTO cards (scryfall_id, name, set_code, collector_number,"
        " oracle_id, type_line, cmc, color_identity)"
        " VALUES ('sol-ring-cmd-1', 'Sol Ring', 'cmd', '1', 'oracle-sol-ring',"
        " 'Artifact', 1, '')"
    )
    conn.execute(
        "INSERT INTO decks (deck_id, name, commander, representative, color_identity)"
        " VALUES (1, 'Smoke Test Deck', 'Sol Ring', 'Sol Ring', '')"
    )
    conn.execute(
        "INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1, 'sol-ring-cmd-1', 1)"
    )
    conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, location) VALUES ('sol-ring-cmd-1', 1, 'Smoke Test Deck')"
    )
    conn.execute(
        "INSERT INTO games (game_id, date) VALUES (1, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO game_participants (game_id, seat, deck_id, deck_name, is_own_deck, is_winner)"
        " VALUES (1, 1, 1, 'Smoke Test Deck', 1, 1)"
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _point_db_at_smoke_db(smoke_db_path, monkeypatch):
    from dashboard_lib import db

    monkeypatch.setattr(db, "DB_PATH", smoke_db_path)


@pytest.mark.parametrize("page_file", [f for f in PAGE_FILES if f not in ROUTER_ONLY])
def test_page_runs_without_exception(page_file):
    # Default AppTest timeout (3s) is tight for pages that cold-import
    # heavy deps (e.g. the Advisor page's model SDKs); give every page
    # room rather than special-casing the slow ones.
    at = AppTest.from_file(os.path.join(PAGES_DIR, page_file), default_timeout=15)
    at.run()
    assert not at.exception


@pytest.mark.parametrize("page_file", PAGE_FILES)
def test_page_runs_through_router(page_file):
    at = AppTest.from_file(os.path.join(PROJECT_ROOT, "dashboard.py"), default_timeout=15)
    at.run()
    assert not at.exception
    at.switch_page(f"pages/{page_file}")
    at.run()
    assert not at.exception
