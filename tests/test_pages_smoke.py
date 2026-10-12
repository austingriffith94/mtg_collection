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

Phase 2 added a second deck plus tests that the shared current deck
(card_view.CURRENT_DECK_KEY) follows you between the per-deck pages and
that the Deck Editor's create flow selects the new deck.
"""
import os
import shutil
import sqlite3

import pytest
import streamlit as st
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
        "INSERT INTO decks (deck_id, name, commander, representative, color_identity)"
        " VALUES (2, 'Second Deck', 'Sol Ring', 'Sol Ring', '')"
    )
    conn.execute(
        "INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (2, 'sol-ring-cmd-1', 1)"
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


# Per-deck pages and their picker widget keys (Phase 2).
DECK_PICKERS = {
    "pages/2_Decks.py": "deckpage_deck_select",
    "pages/3_Land_Probability.py": "landprob_deck_select",
    "pages/4_Deck_Editor.py": "editor_deck_select",
}


@pytest.mark.parametrize("page_path,picker_key", DECK_PICKERS.items())
def test_page_opens_on_current_deck(page_path, picker_key):
    at = AppTest.from_file(os.path.join(PROJECT_ROOT, page_path), default_timeout=15)
    at.session_state["current_deck_id"] = 2
    at.run()
    assert not at.exception
    assert at.selectbox(key=picker_key).value == 2


def test_current_deck_carries_across_pages():
    at = AppTest.from_file(os.path.join(PROJECT_ROOT, "dashboard.py"), default_timeout=15)
    at.run()
    at.switch_page("pages/2_Decks.py")
    at.run()
    at.selectbox(key="deckpage_deck_select").set_value(2).run()
    assert not at.exception
    for page_path, picker_key in DECK_PICKERS.items():
        at.switch_page(page_path)
        at.run()
        assert not at.exception
        assert at.selectbox(key=picker_key).value == 2, page_path


def test_deck_param_seeds_once_then_manual_pick_wins():
    at = AppTest.from_file(os.path.join(PAGES_DIR, "2_Decks.py"), default_timeout=15)
    at.query_params["deck_id"] = "2"
    at.run()
    assert at.selectbox(key="deckpage_deck_select").value == 2
    at.selectbox(key="deckpage_deck_select").set_value(1).run()
    assert at.session_state["current_deck_id"] == 1


def test_create_deck_selects_new_deck(smoke_db_path, tmp_path, monkeypatch):
    # Own copy of the DB: this test writes a deck the other tests don't expect.
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    monkeypatch.setattr(db, "DB_PATH", path)
    # get_connection() is a cache_resource and load_decks_df's cache ignores
    # the connection, so drop both caches before (to open this copy) and
    # after (so later tests reopen the shared smoke DB).
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "4_Deck_Editor.py"), default_timeout=15)
        at.run()
        # "Create a new deck" is an st.dialog (Phase 5) — open it first.
        at.sidebar.button(key="editor_newdeck_open").click().run()
        at.text_input(key="editor_newdeck_name").set_value("Brand New Deck")
        at.button(key="editor_newdeck_create").click().run()
        assert not at.exception
        new_id = sqlite3.connect(path).execute(
            "SELECT deck_id FROM decks WHERE name = 'Brand New Deck'"
        ).fetchone()[0]
        assert at.session_state["current_deck_id"] == new_id
        assert at.selectbox(key="editor_deck_select").value == new_id
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


# Phase 5: the delete-game dialog adds a confirm checkbox the old inline
# expander didn't have — cancel must leave the game, confirming removes it.
def test_delete_game_dialog_cancel_leaves_game_confirm_removes_it(smoke_db_path, tmp_path, monkeypatch):
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "7_Commander_Game_Tracking.py"), default_timeout=15)
        at.run()
        at.button(key="gt_delete_open").click().run()
        # The delete button stays disabled until the confirm checkbox is ticked.
        assert at.button(key="gt_delete_btn").disabled
        at.button(key="gt_delete_cancel").click().run()
        assert not at.exception
        assert sqlite3.connect(path).execute(
            "SELECT COUNT(*) FROM games WHERE game_id = 1"
        ).fetchone()[0] == 1

        at.button(key="gt_delete_open").click().run()
        at.checkbox(key="gt_delete_confirm").set_value(True).run()
        at.button(key="gt_delete_btn").click().run()
        assert not at.exception
        assert sqlite3.connect(path).execute(
            "SELECT COUNT(*) FROM games WHERE game_id = 1"
        ).fetchone()[0] == 0
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


# Phase 3: the Decks page is split into tabs; Compare-all-decks lives on Home.
def test_decks_page_has_tabs_for_every_deck():
    at = AppTest.from_file(os.path.join(PAGES_DIR, "2_Decks.py"), default_timeout=15)
    at.run()
    for deck_id in (1, 2):
        at.session_state["current_deck_id"] = deck_id
        at.run()
        assert not at.exception, deck_id
        labels = [t.label for t in at.tabs]
        for expected in ("Overview", "Mana & Curve", "Combos", "Cards", "History"):
            assert expected in labels, (deck_id, labels)


def test_compare_all_decks_moved_to_home():
    home = AppTest.from_file(os.path.join(PROJECT_ROOT, "dashboard.py"), default_timeout=15)
    home.run()
    assert "📊 Compare all decks" in [e.label for e in home.expander]
    decks = AppTest.from_file(os.path.join(PAGES_DIR, "2_Decks.py"), default_timeout=15)
    decks.run()
    assert "📊 Compare all decks" not in [e.label for e in decks.expander]


def test_decks_page_brewing_deck_and_empty_deck(smoke_db_path, tmp_path, monkeypatch):
    # Own DB copy: deck 2 becomes a brewing deck (Build Plan open, Sol Ring
    # sits in deck 1's sleeve) and deck 3 has no cards at all.
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE decks SET build_state = 'brewing' WHERE deck_id = 2")
    conn.execute(
        "INSERT INTO decks (deck_id, name, commander, color_identity) VALUES (3, 'Empty Deck', NULL, '')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "2_Decks.py"), default_timeout=15)
        for deck_id in (2, 3):
            at.session_state["current_deck_id"] = deck_id
            at.run()
            assert not at.exception, deck_id
            assert "Overview" in [t.label for t in at.tabs]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()
