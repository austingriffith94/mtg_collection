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
import sys

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


@pytest.fixture(autouse=True)
def _fresh_deck_board_component():
    # The Deck Editor mounts the deck board by default, and the component
    # registers itself at import into whichever Streamlit Runtime exists
    # then — AppTest builds a new one per run (see _forget_deck_board).
    _forget_deck_board()


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


def _forget_deck_board():
    """Make the next page run re-import deck_board (and so re-register its
    component). `from dashboard_lib import deck_board` consults the package
    attribute before sys.modules, so both have to go."""
    import dashboard_lib

    sys.modules.pop("dashboard_lib.deck_board", None)
    if hasattr(dashboard_lib, "deck_board"):
        del dashboard_lib.deck_board


def test_decks_page_stacks_view(smoke_db_path, tmp_path, monkeypatch):
    # VISUAL_DECK_PLAN.md Phase A: the Cards tab's View gains "Stacks" for the
    # Mainboard. AppTest can't click inside the component's JS, so this only
    # proves the page renders with the view selected — for a deck with cards
    # (no cached image, so the Scryfall/None fallback path) and an empty one.
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO decks (deck_id, name, commander, color_identity) VALUES (3, 'Empty Deck', NULL, '')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    # The component registers itself at import, in the Streamlit Runtime that
    # exists then. A real server has one Runtime for the whole process, but
    # every AppTest.run() builds a fresh one, so re-import to re-register
    # (done again before the second run below).
    _forget_deck_board()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "2_Decks.py"), default_timeout=15)
        for deck_id in (1, 3):
            at.session_state["current_deck_id"] = deck_id
            at.run()
            assert not at.exception, deck_id
            if deck_id != 1:
                continue  # the empty deck shows "no cards" instead of a browser
            view = at.segmented_control(key="deck_1_Mainboard_view")
            assert "Stacks" in view.options
            _forget_deck_board()
            view.set_value("Stacks").run()
            assert not at.exception, deck_id
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_deck_editor_board_tab(smoke_db_path, tmp_path, monkeypatch):
    # VISUAL_DECK_PLAN.md Phase B: the Deck Editor's first tab is the editable
    # Board. Renders for a deck with cards, an empty deck and (the no-deck
    # case is covered by test_page_runs_without_exception); the add bar adds
    # a card and the board's stats strip follows. AppTest can't click inside
    # the component or set a trigger value, so edits themselves are covered
    # by the apply_board_action tests in test_deck_board.py.
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO decks (deck_id, name, commander, color_identity) VALUES (3, 'Empty Deck', NULL, '')"
    )
    conn.execute(
        "INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, type_line, cmc)"
        " VALUES ('arcane-sign-1', 'Arcane Signet', 'cmd', '2', 'oracle-arcane', 'Artifact', 2)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    _forget_deck_board()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "4_Deck_Editor.py"), default_timeout=15)
        for deck_id in (1, 3):
            at.session_state["current_deck_id"] = deck_id
            _forget_deck_board()
            at.run()
            assert not at.exception, deck_id
            assert [t.label for t in at.tabs][0] == "🃏 Board"
        # The Undo button reads the same session key run_board_action() writes
        # (the component's key). A pending undo seeded under that key must
        # produce the button on the page — this caught a key mismatch the
        # scripted-app test above couldn't, because it used one key for both.
        at.session_state["current_deck_id"] = 1
        at.session_state["editor_1_board_stacks__undo"] = {
            "label": "Removed Sol Ring", "action": {"op": "set_qty", "id": "sol-ring-cmd-1", "qty": 1},
        }
        _forget_deck_board()
        at.run()
        assert not at.exception
        assert at.button(key="editor_1_board_stacks__undo_btn").label == "↶ Undo: Removed Sol Ring"
        at.session_state["current_deck_id"] = 3
        _forget_deck_board()
        at.run()
        # Back on the empty deck: add a card through the Board's add bar.
        at.text_input(key="editor_3_board_add_name").set_value("Arcane Signet")
        _forget_deck_board()
        at.button(key="editor_3_board_add_btn").click().run()
        assert not at.exception
        assert not at.error
        count = sqlite3.connect(path).execute(
            "SELECT quantity FROM deck_cards WHERE deck_id=3 AND scryfall_id='arcane-sign-1'"
        ).fetchone()
        assert count == (1,)
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_board_edit_glue_undo_button(smoke_db_path, tmp_path, monkeypatch):
    # AppTest can't send a component trigger, so drive the page-side glue
    # (deck_board.run_board_action + the Undo button) from a tiny script:
    # an edit toasts, stores its inverse and flags the rerun; the Undo button
    # reverses it and then disappears (one level); a refused edit toasts and
    # leaves the previous undo alone.
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()

    def script():
        import streamlit as st
        from dashboard_lib import db, deck_board

        conn = db.get_connection()
        if st.session_state.get("edit"):
            deck_board.run_board_action(conn, 1, "k", st.session_state.pop("edit"))
        deck_board.render_undo_button(conn, 1, "k")

    def qty():
        return sqlite3.connect(path).execute(
            "SELECT quantity FROM deck_cards WHERE deck_id=1 AND scryfall_id='sol-ring-cmd-1'"
        ).fetchone()

    try:
        at = AppTest.from_function(script, default_timeout=15)
        at.session_state["edit"] = {"op": "inc", "id": "sol-ring-cmd-1"}
        at.run()
        assert not at.exception
        assert qty() == (2,)
        assert at.session_state["k__edited"] is True
        assert at.button(key="k__undo_btn").label.startswith("↶ Undo: ")

        at.session_state["edit"] = {"op": "inc", "id": "no-such-card"}  # refused
        at.run()
        assert qty() == (2,) and "k__undo_btn" in [b.key for b in at.button]

        at.button(key="k__undo_btn").click().run()
        assert not at.exception
        assert qty() == (1,)
        assert "k__undo_btn" not in [b.key for b in at.button]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_deck_editor_board_tab_with_shortlist(smoke_db_path, tmp_path, monkeypatch):
    # VISUAL_DECK_PLAN.md Phase C: the Board carries the Shortlist (with
    # cross-deck verdicts) once the deck has ideas, then a planned swap. The
    # empty-Shortlist case is test_deck_editor_board_tab above.
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, type_line, cmc)"
        " VALUES ('arcane-sign-1', 'Arcane Signet', 'cmd', '2', 'oracle-arcane', 'Artifact', 2)"
    )
    conn.execute(
        "INSERT INTO deck_changes (deck_id, status, add_scryfall_id, quantity) VALUES (1, 'idea', 'arcane-sign-1', 1)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "4_Deck_Editor.py"), default_timeout=15)
        at.session_state["current_deck_id"] = 1
        _forget_deck_board()
        at.run()
        assert not at.exception

        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE deck_changes SET status='planned', planned_at=CURRENT_TIMESTAMP,"
            " remove_scryfall_id='sol-ring-cmd-1' WHERE add_scryfall_id='arcane-sign-1'"
        )
        conn.commit()
        conn.close()
        st.cache_data.clear()
        _forget_deck_board()
        at.run()
        assert not at.exception
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_board_glue_plan_then_apply(smoke_db_path, tmp_path, monkeypatch):
    # Phase C's ops through the page glue: `plan` leaves an Undo (unpair);
    # `apply` has no inverse, so it clears that Undo rather than leaving one
    # that would act on a deck that has moved on.
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, type_line, cmc)"
        " VALUES ('arcane-sign-1', 'Arcane Signet', 'cmd', '2', 'oracle-arcane', 'Artifact', 2)"
    )
    change_id = conn.execute(
        "INSERT INTO deck_changes (deck_id, status, add_scryfall_id, quantity) VALUES (1, 'idea', 'arcane-sign-1', 1)"
    ).lastrowid
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()

    def script():
        import streamlit as st
        from dashboard_lib import db, deck_board

        conn = db.get_connection()
        if st.session_state.get("edit"):
            deck_board.run_board_action(conn, 1, "k", st.session_state.pop("edit"))
        deck_board.render_undo_button(conn, 1, "k")

    def status():
        return sqlite3.connect(path).execute(
            "SELECT status FROM deck_changes WHERE change_id=?", (change_id,)
        ).fetchone()[0]

    try:
        at = AppTest.from_function(script, default_timeout=15)
        at.session_state["edit"] = {"op": "plan", "change_id": change_id, "cut_id": "sol-ring-cmd-1"}
        at.run()
        assert not at.exception
        assert status() == "planned"
        assert at.button(key="k__undo_btn").label.startswith("↶ Undo: Planned")

        at.session_state["edit"] = {"op": "apply", "change_id": change_id}
        at.run()
        assert not at.exception
        assert status() == "applied"
        assert "k__undo_btn" not in [b.key for b in at.button]
        assert sqlite3.connect(path).execute(
            "SELECT scryfall_id FROM deck_cards WHERE deck_id=1"
        ).fetchall() == [("arcane-sign-1",)]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()
