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
        assert [t.body for t in at.toast] == ["Game deleted."]  # via finish_edit (Phase 0)
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
        # Stashed for setup_page() to show on the full rerun (card_view.stash_edit).
        assert at.session_state["_pending_feedback"]["lines"] == ["Sol Ring → ×2"]
        assert at.button(key="k__undo_btn").label.startswith("↶ Undo: ")
        # EDIT_FLOW_PLAN.md Phase 1: the deck's only copy is already sleeved
        # and deck 2 has none to give, so +1 makes a starter lot here — and
        # the Undo has to delete it again, not just lower the quantity.
        lots = lambda: sqlite3.connect(path).execute(  # noqa: E731
            "SELECT location, COUNT(*) FROM collection GROUP BY location").fetchall()
        assert lots() == [("Smoke Test Deck", 2)]
        assert [m["kind"] for m in at.session_state["_pending_feedback"]["moves"]] == ["new"]

        at.session_state["edit"] = {"op": "inc", "id": "no-such-card"}  # refused
        at.run()
        assert qty() == (2,) and "k__undo_btn" in [b.key for b in at.button]

        at.button(key="k__undo_btn").click().run()
        assert not at.exception
        assert qty() == (1,)
        assert lots() == [("Smoke Test Deck", 1)]
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


# Phase 0 of EDIT_FLOW_PLAN.md: card_view.finish_edit()/show_pending_feedback()
# replaced the Deck Editor's hand-rolled invalidate/toast/rerun triples —
# these two prove a rename and a Shortlist "add idea" still write, still
# show their message (now a toast surfaced on the NEXT run), and the page
# still has no exception across that rerun.
def test_deck_editor_rename_shows_toast_after_finish_edit(smoke_db_path, tmp_path, monkeypatch):
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "4_Deck_Editor.py"), default_timeout=15)
        at.session_state["current_deck_id"] = 1
        _forget_deck_board()
        at.run()
        at.text_input(key="editor_1_name").set_value("Renamed Deck")
        _forget_deck_board()
        at.button(key="editor_1_savename").click().run()
        assert not at.exception
        assert sqlite3.connect(path).execute(
            "SELECT name FROM decks WHERE deck_id=1"
        ).fetchone() == ("Renamed Deck",)
        assert any("Renamed to 'Renamed Deck'" in t.body for t in at.toast)
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_deck_editor_shortlist_add_idea_shows_toast_after_finish_edit(smoke_db_path, tmp_path, monkeypatch):
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "4_Deck_Editor.py"), default_timeout=15)
        at.session_state["current_deck_id"] = 1
        _forget_deck_board()
        at.run()
        at.text_input(key="editor_1_sl_add_name").set_value("Sol Ring")  # already a known card, no network
        _forget_deck_board()
        submit = next(b for b in at.button if b.label == "➕ Add to shortlist")
        submit.click().run()
        assert not at.exception
        assert sqlite3.connect(path).execute(
            "SELECT COUNT(*) FROM deck_changes WHERE deck_id=1 AND status='idea'"
        ).fetchone() == (1,)
        assert any("Added Sol Ring to the shortlist" in t.body for t in at.toast)
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_workbench_contention_drop_writes_and_shows_message(smoke_db_path, tmp_path, monkeypatch):
    # The smoke DB runs Sol Ring in both decks on one owned copy, so it's in
    # contention. Dropping it from a deck goes through the Workbench's
    # _refresh() -> card_view.finish_edit() (EDIT_FLOW_PLAN.md Phase 0).
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "8_Workbench.py"), default_timeout=15)
        at.run()
        assert not at.exception
        at.selectbox(key="workbench_contention_drop_pick_oracle-sol-ring_0").set_value("Second Deck")
        at.button(key="workbench_contention_drop_btn_oracle-sol-ring_0").click().run()
        assert not at.exception
        assert sqlite3.connect(path).execute(
            "SELECT COUNT(*) FROM deck_cards WHERE deck_id=2"
        ).fetchone() == (0,)
        assert [t.body for t in at.toast] == ["Dropped Sol Ring from Second Deck."]
        assert at.session_state["workbench_ver"] == 1  # editors re-keyed after the write
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_collection_editor_location_catalog_add_shows_message(smoke_db_path, tmp_path, monkeypatch):
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "5_Collection_Editor.py"), default_timeout=15)
        at.run()
        at.button(key="editor_loc_catalog_open").click().run()
        at.text_input(key="editor_loc_catalog_add").set_value("Binder 2")
        at.button(key="editor_loc_catalog_add_btn").click().run()
        assert not at.exception
        assert sqlite3.connect(path).execute(
            "SELECT COUNT(*) FROM location_catalog WHERE location='Binder 2'"
        ).fetchone() == (1,)
        assert [t.body for t in at.toast] == ["Added 'Binder 2' to the master list."]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


# ------------------------------------------------------------------
# EDIT_FLOW_PLAN.md Phase 1: direct edits move copies, and the pop-up says
# what moved — a toast for one or two moves, a dialog listing more.
# ------------------------------------------------------------------
def _signet_db(smoke_db_path, tmp_path, extra_sql=()):
    """A smoke DB copy with Arcane Signet owned in Box and an empty deck 3."""
    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, type_line, cmc)"
        " VALUES ('arcane-sign-1', 'Arcane Signet', 'cmd', '2', 'oracle-arcane', 'Artifact', 2)"
    )
    conn.execute("INSERT INTO collection (scryfall_id, quantity, location) VALUES ('arcane-sign-1', 1, 'Box')")
    conn.execute("INSERT INTO decks (deck_id, name, commander, color_identity) VALUES (3, 'Empty Deck', NULL, '')")
    for sql in extra_sql:
        conn.execute(sql)
    conn.commit()
    conn.close()
    return path


def _location_of(path, scryfall_id):
    return [r[0] for r in sqlite3.connect(path).execute(
        "SELECT location FROM collection WHERE scryfall_id=? ORDER BY collection_id", (scryfall_id,))]


def test_deck_editor_add_bar_moves_the_box_copy_and_says_so(smoke_db_path, tmp_path, monkeypatch):
    from dashboard_lib import db

    path = _signet_db(smoke_db_path, tmp_path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "4_Deck_Editor.py"), default_timeout=15)
        at.session_state["current_deck_id"] = 3
        _forget_deck_board()
        at.run()
        at.text_input(key="editor_3_board_add_name").set_value("Arcane Signet")
        _forget_deck_board()
        at.button(key="editor_3_board_add_btn").click().run()
        assert not at.exception
        # The Box copy was moved, not a starter lot made beside it.
        assert _location_of(path, "arcane-sign-1") == ["Empty Deck"]
        bodies = [t.body for t in at.toast]
        assert "Added 1x Arcane Signet." in bodies
        assert "Arcane Signet: Box → Empty Deck" in bodies
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_deck_editor_mainboard_save_lists_every_move_in_a_dialog(smoke_db_path, tmp_path, monkeypatch):
    # Three sleeved cards deleted in one save: three moves, so a dialog
    # grouped by from → to rather than a pile of toasts.
    from dashboard_lib import db

    extra = []
    for i, name in enumerate(("Mind Stone", "Fellwar Stone", "Thought Vessel"), start=10):
        extra += [
            f"INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, type_line, cmc)"
            f" VALUES ('rock-{i}', '{name}', 'cmd', '{i}', 'oracle-{i}', 'Artifact', 2)",
            f"INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (3, 'rock-{i}', 1)",
            f"INSERT INTO collection (scryfall_id, quantity, location) VALUES ('rock-{i}', 1, 'Empty Deck')",
        ]
    path = _signet_db(smoke_db_path, tmp_path, extra)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "4_Deck_Editor.py"), default_timeout=15)
        at.session_state["current_deck_id"] = 3
        _forget_deck_board()
        at.run()
        at.session_state["editor_3_main_editor"] = {
            "edited_rows": {i: {"Delete": True} for i in range(3)}, "added_rows": [], "deleted_rows": [],
        }
        _forget_deck_board()
        at.button(key="editor_3_main_save").click().run()
        assert not at.exception
        for i in (10, 11, 12):
            assert _location_of(path, f"rock-{i}") == ["Box"]
        text = "\n".join(m.value for m in at.markdown)
        assert "**Empty Deck → Box**" in text
        for name in ("Mind Stone", "Fellwar Stone", "Thought Vessel"):
            assert f"- {name}" in text
        assert [t.body for t in at.toast] == ["Updated 3 card(s)."]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_mark_deck_built_says_what_is_still_to_pull(smoke_db_path, tmp_path, monkeypatch):
    # Deck 2 is brewing and its only copy of Sol Ring is in deck 1's sleeves,
    # so one card is still to pull; clearing the flag says so.
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE decks SET build_state = 'brewing' WHERE deck_id = 2")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    note = "1 card(s) are still to pull. They'll stay on Reconcile until you sleeve them."
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "2_Decks.py"), default_timeout=15)
        at.session_state["current_deck_id"] = 2
        at.run()
        assert not at.exception
        assert note in [c.value for c in at.caption]
        at.button(key="deck_2_mark_built").click().run()
        assert not at.exception
        assert sqlite3.connect(path).execute(
            "SELECT build_state FROM decks WHERE deck_id=2").fetchone() == (None,)
        assert [t.body for t in at.toast] == ["Marked as built.", note]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_workbench_sleeved_here_reports_the_move(smoke_db_path, tmp_path, monkeypatch):
    # Reconcile's "Sleeved here" is a lot-only move (no list change), and it
    # reports through finish_edit() like a deck edit: deck 2 is short Sol Ring,
    # whose one copy sits in deck 1.
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    shutil.copy(smoke_db_path, path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "8_Workbench.py"), default_timeout=15)
        at.run()
        at.button(key="wb_pull_2_oracle-sol-ring_0").click().run()
        assert not at.exception
        assert _location_of(path, "sol-ring-cmd-1") == ["Second Deck"]
        assert [t.body for t in at.toast] == [
            "Sol Ring located to Second Deck.", "Sol Ring: Smoke Test Deck → Second Deck"]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


# ------------------------------------------------------------------
# EDIT_FLOW_PLAN.md Phase 6: a Collection-side save that moves a lot into a
# deck asks whether the deck's list should follow, and every lot change is in
# the move log that Card Lookup shows.
# ------------------------------------------------------------------
def _collection_edit_into_empty_deck(path):
    """Collection page, edit mode, Arcane Signet's Location -> 'Empty Deck'
    (the two lots sort Signet first, then Sol Ring), save clicked. Returns the
    AppTest after the save run."""
    at = AppTest.from_file(os.path.join(PAGES_DIR, "1_Collection.py"), default_timeout=15)
    at.run()
    at.toggle(key="collection_edit_mode").set_value(True).run()
    editor_key = f"collection_edit_table_{hash((2, 1))}"
    at.session_state[editor_key] = {
        "edited_rows": {0: {"Location": "Empty Deck"}}, "added_rows": [], "deleted_rows": [],
    }
    # The Save button renders disabled until a run has seen the edit, and
    # AppTest refuses to click a disabled widget — but a second run would also
    # drop the editor state set above (AppTest only carries it into one run).
    # So enable the button's proto by hand, the way a browser would have after
    # the edit.
    save = at.button(key="collection_edit_save")
    save.proto.disabled = False
    save.click().run()
    return at


def _list_of(path, deck_id):
    return sqlite3.connect(path).execute(
        "SELECT scryfall_id, quantity FROM deck_cards WHERE deck_id=?", (deck_id,)).fetchall()


def test_collection_save_into_a_deck_saves_then_asks_about_the_list(smoke_db_path, tmp_path, monkeypatch):
    from dashboard_lib import db

    path = _signet_db(smoke_db_path, tmp_path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = _collection_edit_into_empty_deck(path)
        assert not at.exception
        # The save itself already happened and is in the move log...
        assert _location_of(path, "arcane-sign-1") == ["Empty Deck"]
        assert sqlite3.connect(path).execute(
            "SELECT from_location, to_location, reason FROM collection_moves").fetchall() == [
            ("Box", "Empty Deck", "collection_edit")]
        # ...and the dialog asks about the deck's list, which hasn't changed.
        assert _list_of(path, 3) == []
        text = "\n".join(m.value for m in at.markdown)
        assert "- Add Arcane Signet ×1 (list ×0 → ×1)" in text
        assert [b.key for b in at.button if (b.key or "").startswith("_deck_sync_")] == [
            "_deck_sync_yes_Empty Deck", "_deck_sync_no_Empty Deck"]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def _deck_sync_dialog_script():
    # A page that shows the dialog on every run. In the app it opens once from
    # the stashed feedback, and a click inside it reruns only the dialog;
    # AppTest reruns the whole script, so this stands in for that.
    import streamlit as st
    from dashboard_lib import card_view, db, queries

    conn = db.get_connection()
    st.session_state.pop("_pending_feedback", None)
    card_view.show_deck_sync(queries.deck_sync_offers(conn, 0))


def _deck_sync_world(smoke_db_path, tmp_path):
    """Arcane Signet moved Box -> Empty Deck by a logged collection edit, whose
    list doesn't run it yet (the state the dialog is asked in)."""
    path = _signet_db(smoke_db_path, tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE collection SET location='Empty Deck' WHERE scryfall_id='arcane-sign-1'")
    conn.execute(
        "INSERT INTO collection_moves (collection_id, scryfall_id, card_name, quantity, from_location,"
        " to_location, reason) VALUES (2, 'arcane-sign-1', 'Arcane Signet', 1, 'Box', 'Empty Deck',"
        " 'collection_edit')")
    conn.commit()
    conn.close()
    return path


def test_deck_sync_dialog_yes_updates_the_list_without_moving_the_lot_again(smoke_db_path, tmp_path, monkeypatch):
    from dashboard_lib import db

    path = _deck_sync_world(smoke_db_path, tmp_path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_function(_deck_sync_dialog_script, default_timeout=15)
        at.run()
        assert not at.exception
        at.button(key="_deck_sync_yes_Empty Deck").click().run()
        assert not at.exception
        assert _list_of(path, 3) == [("arcane-sign-1", 1)]
        assert _location_of(path, "arcane-sign-1") == ["Empty Deck"]
        assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM collection_moves").fetchone() == (1,)
        assert sqlite3.connect(path).execute(
            "SELECT status, notes FROM deck_changes WHERE deck_id=3").fetchall() == [
            ("applied", "following a collection edit")]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_deck_sync_dialog_just_the_collection_leaves_the_list(smoke_db_path, tmp_path, monkeypatch):
    from dashboard_lib import db

    path = _deck_sync_world(smoke_db_path, tmp_path)
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_function(_deck_sync_dialog_script, default_timeout=15)
        at.run()
        at.button(key="_deck_sync_no_Empty Deck").click().run()
        assert not at.exception
        assert _list_of(path, 3) == []
        assert _location_of(path, "arcane-sign-1") == ["Empty Deck"]
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()


def test_card_lookup_shows_where_copies_went(smoke_db_path, tmp_path, monkeypatch):
    from dashboard_lib import db

    path = _signet_db(smoke_db_path, tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO collection_moves (card_name, quantity, from_location, to_location, reason, moved_at)"
        " VALUES ('Arcane Signet', 1, 'Box', 'Empty Deck', 'deck_edit', '2026-10-13 09:00:00')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    try:
        at = AppTest.from_file(os.path.join(PAGES_DIR, "9_Card_Lookup.py"), default_timeout=15)
        at.run()
        at.text_input(key="card_lookup_search").set_value("Arcane Signet").run()
        assert not at.exception
        captions = [c.value for c in at.caption]
        assert "Where copies went" in captions
        assert "2026-10-13  ×1 Box → Empty Deck (deck edit)" in captions
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()
