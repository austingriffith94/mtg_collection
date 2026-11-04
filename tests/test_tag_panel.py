"""The tag review panel (docs/archive/TAG_SUGGESTIONS_PLAN.md Phase 4): Apply and Undo,
the rows that start ticked, definition drafts, and an AppTest pass over the pop-up
with a scripted provider. Offline; the pages run against a temp database."""
import json
import os
import shutil
import sqlite3
import sys

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from dashboard_lib import queries as q, tag_panel, tag_vocab, writes
from dashboard_lib.advisor import guard, providers, tagging
from dashboard_lib.advisor.context import build_tag_context
from dashboard_lib.advisor.providers import FakeProvider, ProviderError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDITOR = os.path.join(PROJECT_ROOT, "pages", "4_Deck_Editor.py")

CARDS = {
    "Sol Ring": ("{T}: Add {C}{C}.", "Artifact"),
    "Lightning Bolt": ("Lightning Bolt deals 3 damage to any target.", "Instant"),
    "Arcane Signet": ("{T}: Add one mana.", "Artifact"),
    "Mind Stone": ("{T}: Add {C}.", "Artifact"),
    "Night's Whisper": ("You draw two cards and you lose 2 life.", "Sorcery"),
}


def _seed(conn):
    conn.execute("INSERT INTO decks (deck_id, name, commander, color_identity, description)"
                 " VALUES (1, 'Test', 'Sol Ring', 'BR', 'Aggro')")
    ids = {}
    for name, (text, type_line) in CARDS.items():
        sid = name.split()[0].lower().replace("'s", "")
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, oracle_text,"
                     " type_line, mana_cost, is_basic_land) VALUES (?,?,'cmd','1',?,?,?,'{1}',0)",
                     (sid, name, f"o-{sid}", text, type_line))
        conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1,?,1)", (sid,))
        ids[name] = sid
    conn.commit()
    return ids


@pytest.fixture
def ids(conn):
    ids = _seed(conn)
    writes.set_card_tags(conn, 1, ids["Sol Ring"], "function", ["Ramp"])
    return ids


def _row(card, tags, seen, replace=False):
    return {"card": card, "tags": tags, "replace": replace, "seen": seen}


def _tags(conn, sid):
    return q.get_card_tags_by_type(conn, 1, "function").get(sid, [])


# ---- writes.apply_tag_proposals / undo_tag_proposals ------------------------

def test_apply_adds_after_the_primary_and_counts(conn, ids):
    sol, bolt = ids["Sol Ring"], ids["Lightning Bolt"]
    result = writes.apply_tag_proposals(conn, 1, "function", [
        _row("Sol Ring", ["Card Adv", "Ramp"], {sol: ["Ramp"]}),
        _row("Lightning Bolt", ["Removal", "Interaction"], {bolt: []}),
    ])
    assert _tags(conn, sol) == ["Ramp", "Card Adv"]        # keeps its primary
    assert _tags(conn, bolt) == ["Removal", "Interaction"]  # takes the model's
    assert (result["tags_added"], result["cards"], result["replaced"]) == (3, 2, 0)


def test_replace_swaps_the_tags_and_backs_up_only_then(conn, ids, monkeypatch):
    calls = []
    monkeypatch.setattr(writes.backup, "snapshot", lambda *a: calls.append(a[1:]) or "path")
    sol = ids["Sol Ring"]
    writes.apply_tag_proposals(conn, 1, "function", [_row("Sol Ring", ["Fixing"], {sol: ["Ramp"]})])
    assert calls == []
    result = writes.apply_tag_proposals(conn, 1, "function", [
        _row("Sol Ring", ["Card Adv"], {sol: ["Ramp", "Fixing"]}, replace=True)])
    assert _tags(conn, sol) == ["Card Adv"]
    assert calls == [("before", "replace-tags")]
    assert result["backup_path"] == "path" and result["replaced"] == 1


def test_apply_skips_a_card_edited_or_gone_since_suggest(conn, ids):
    sol, bolt = ids["Sol Ring"], ids["Lightning Bolt"]
    writes.set_card_tags(conn, 1, sol, "function", ["Fixing"])
    conn.execute("DELETE FROM deck_cards WHERE scryfall_id=?", (bolt,))
    conn.commit()
    result = writes.apply_tag_proposals(conn, 1, "function", [
        _row("Sol Ring", ["Card Adv"], {sol: ["Ramp"]}, replace=True),
        _row("Lightning Bolt", ["Removal"], {bolt: []}),
    ])
    assert _tags(conn, sol) == ["Fixing"]
    assert _tags(conn, bolt) == []
    assert result["skipped"] == [("Sol Ring", "its tags changed since Suggest"),
                                 ("Lightning Bolt", "no longer on the mainboard")]
    assert result["changes"] == []


def test_apply_is_all_or_nothing(conn, ids, monkeypatch):
    real = writes.get_or_create_tag

    def fail_on_removal(conn, tag_type, label):
        if label == "Removal":
            raise sqlite3.OperationalError("disk full")
        return real(conn, tag_type, label)

    monkeypatch.setattr(writes, "get_or_create_tag", fail_on_removal)
    with pytest.raises(sqlite3.OperationalError):
        writes.apply_tag_proposals(conn, 1, "function", [
            _row("Arcane Signet", ["Ramp"], {ids["Arcane Signet"]: []}),
            _row("Lightning Bolt", ["Removal"], {ids["Lightning Bolt"]: []}),
        ])
    assert _tags(conn, ids["Arcane Signet"]) == []


def test_apply_writes_every_printing(conn, ids, add_card):
    other = add_card("Sol Ring", set_code="xyz")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (1,?,1)", (other,))
    conn.commit()
    sol = ids["Sol Ring"]
    result = writes.apply_tag_proposals(conn, 1, "function", [
        _row("Sol Ring", ["Fixing"], {sol: ["Ramp"], other: []})])
    assert _tags(conn, sol) == ["Ramp", "Fixing"] and _tags(conn, other) == ["Fixing"]
    assert (result["tags_added"], result["cards"]) == (1, 1)


def test_undo_restores_a_batch_and_leaves_later_edits(conn, ids):
    sol, bolt = ids["Sol Ring"], ids["Lightning Bolt"]
    result = writes.apply_tag_proposals(conn, 1, "function", [
        _row("Sol Ring", ["Card Adv"], {sol: ["Ramp"]}, replace=True),
        _row("Lightning Bolt", ["Removal"], {bolt: []}),
    ])
    writes.set_card_tags(conn, 1, bolt, "function", ["Interaction"])
    assert writes.undo_tag_proposals(conn, 1, "function", result["changes"]) == (1, ["Lightning Bolt"])
    assert _tags(conn, sol) == ["Ramp"]
    assert _tags(conn, bolt) == ["Interaction"]


# ---- what starts ticked -----------------------------------------------------

def _checked(ctx, *rows):
    checked = guard.check_tags({"cards": list(rows)}, ctx)
    for r in checked.rows:
        r.status = tagging.classify(r.tags, ctx.current.get(r.card.name, []))
    return checked.rows


def _answer(card, tags, quote):
    return {"card": card, "tags": tags, "primary": tags[0] if tags else None, "evidence_quote": quote, "reason": "r"}


@pytest.fixture
def ctx(conn, ids):
    for name in ("Arcane Signet", "Mind Stone"):
        writes.set_card_tags(conn, 1, ids[name], "function", ["Ramp"])
    writes.set_tag_vocab_label(conn, 1, "function", "Removal")
    writes.set_tag_vocab_label(conn, 1, "function", "Card Adv")
    return build_tag_context(conn, 1, "function")


def test_first_tag_starts_ticked_but_a_thin_label_does_not(ctx):
    rows = _checked(ctx, _answer("Lightning Bolt", ["Ramp"], "any target"),
                    _answer("Night's Whisper", ["Card Adv"], "draw two cards"))
    ticked, held = tagging.starts_ticked(ctx, rows)
    assert ticked == {"Lightning Bolt"}
    assert held == ["Card Adv"]


def test_secondary_only_conflict_and_unverified_rows_start_unticked(ctx):
    rows = _checked(ctx, _answer("Sol Ring", ["Ramp", "Card Adv"], "Add {C}{C}"),   # secondary only
                    _answer("Mind Stone", ["Removal"], "Add {C}"),                  # conflict
                    _answer("Lightning Bolt", ["Ramp"], "not on the card"))         # unverified
    assert tagging.starts_ticked(ctx, rows) == (set(), [])


def test_a_new_primary_starts_ticked(ctx):
    rows = _checked(ctx, _answer("Sol Ring", ["Card Adv", "Ramp"], "Add {C}{C}"),
                    _answer("Arcane Signet", ["Card Adv", "Ramp"], "Add one mana"),
                    _answer("Mind Stone", ["Card Adv", "Ramp"], "Add {C}"))
    assert tagging.starts_ticked(ctx, rows)[0] == {"Sol Ring", "Arcane Signet", "Mind Stone"}


def test_focus_counts_a_replace_as_losing_the_old_label(ctx):
    rows = _checked(ctx, _answer("Sol Ring", ["Removal"], "Add {C}{C}"))
    assert tagging.focus_report(ctx, rows).counts == {"Ramp": 3, "Removal": 1}
    assert tagging.focus_report(ctx, rows, replaced={"Sol Ring"}).counts == {"Ramp": 2, "Removal": 1}


# ---- definition drafts ------------------------------------------------------

def test_drafts_cover_cards_notes_and_unusable_answers(conn, ids):
    writes.set_card_tags(conn, 1, ids["Sol Ring"], "strategy", ["Rocks"])
    writes.set_card_tags(conn, 1, ids["Mind Stone"], "strategy", ["Rocks"])
    writes.set_card_tags(conn, 1, ids["Lightning Bolt"], "strategy", ["Gisa Help"])
    writes.set_card_tags(conn, 1, ids["Arcane Signet"], "strategy", ["Blank"])
    ctx = build_tag_context(conn, 1, "strategy")
    provider = FakeProvider([{"definitions": [
        {"label": "Rocks", "definition": "Mana\n  artifacts."},
        {"label": "Gisa Help", "definition": "Helps Gisa."},
        {"label": "Invented", "definition": "x"},
        {"label": "Blank", "definition": " "},
    ]}])
    result = tagging.draft_definitions(provider, ctx, ["Rocks", "Gisa Help", "Blank", "Unused"])
    assert result.drafts == {"Rocks": "Mana artifacts.", "Gisa Help": "Helps Gisa."}
    assert result.one_card == ["Gisa Help"]
    assert result.no_cards == ["Unused"]
    assert result.missing == ["Blank"]
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "LABEL: Rocks" in prompt and "Mind Stone" in prompt and "Unused" not in prompt
    assert q.active_tag_vocab(conn, 1, "strategy") == {}    # nothing saved


def test_a_failed_draft_is_reported(conn, ids):
    writes.set_card_tags(conn, 1, ids["Sol Ring"], "strategy", ["Rocks"])

    class Broken:
        def chat(self, *a, **k):
            raise ProviderError("503")

    result = tagging.draft_definitions(Broken(), build_tag_context(conn, 1, "strategy"), ["Rocks"])
    assert "503" in result.error and result.missing == ["Rocks"]


def test_save_vocabulary_keeps_defaults_blank_and_removes(conn, ids):
    writes.set_tag_vocab_label(conn, 1, "function", "Removal", "Mine.")
    writes.set_tag_vocab_label(conn, 1, "function", "Wipe", "Gone soon.")
    ramp_default = q.deck_tag_vocabulary(conn, 1, "function")[0]
    assert ramp_default["label"] == "Ramp"
    changed = tag_panel.save_vocabulary(conn, 1, "function", pd.DataFrame([
        {"Label": "Ramp", "Definition": ramp_default["definition"], "Remove": False},   # untouched
        {"Label": "Removal", "Definition": "Edited.", "Remove": False},
        {"Label": "Wipe", "Definition": "Gone soon.", "Remove": True},
    ]))
    assert changed == 2
    assert q.active_tag_vocab(conn, 1, "function") == {"Removal": "Edited."}


# ---- the pop-up, through the real page --------------------------------------

@pytest.fixture
def page_db(tmp_path, monkeypatch):
    """A file database for the Deck Editor, Ramp defined by three tagged cards."""
    from dashboard_lib import db

    path = str(tmp_path / "mtg_collection.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(PROJECT_ROOT, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    q.ensure_schema(conn)
    ids = _seed(conn)
    for name in ("Sol Ring", "Arcane Signet", "Mind Stone"):
        writes.set_card_tags(conn, 1, ids[name], "function", ["Ramp"])
    writes.set_tag_vocab_label(conn, 1, "function", "Removal")
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    st.cache_resource.clear()
    st.cache_data.clear()
    yield path
    st.cache_resource.clear()
    st.cache_data.clear()


def _forget_board():
    import dashboard_lib

    sys.modules.pop("dashboard_lib.deck_board", None)
    if hasattr(dashboard_lib, "deck_board"):
        del dashboard_lib.deck_board


def _run(at):
    _forget_board()
    at.run()
    assert not at.exception
    return at


def _rows(path):
    return sqlite3.connect(path).execute(
        """SELECT c.name, t.label FROM card_tags ct JOIN tags t USING (tag_id) JOIN cards c USING (scryfall_id)
           WHERE t.tag_type = 'function' ORDER BY c.name, ct.position""").fetchall()


GOOD = {"cards": [
    _answer("Lightning Bolt", ["Ramp"], "any target"),                 # first tag: ticked
    _answer("Night's Whisper", ["Removal"], "draw two cards"),         # a thin label: unticked
    _answer("Sol Ring", ["Removal"], "Add {C}{C}"),                    # conflict: unticked
    _answer("Arcane Signet", ["Ramp"], "Add one mana"),                # same: hidden
    _answer("Mind Stone", [], ""),
]}


def _open_and_suggest(monkeypatch, replies):
    fake = FakeProvider(list(replies))
    monkeypatch.setattr(providers, "make_provider", lambda choice: fake)
    at = AppTest.from_file(EDITOR, default_timeout=20)
    at.session_state["current_deck_id"] = 1
    _run(at)
    at.selectbox(key="editor_1_cardtag_type_choice").set_value("function")
    _run(at)
    _forget_board()
    at.button(key="tag_panel_1_open_btn").click()
    _run(at)
    _forget_board()
    at.button(key="tag_panel_1_function_suggest").click()
    _run(at)
    return at, fake


def _table_key(at):
    return "tag_panel_1_function_table_{}".format(at.session_state["tag_proposals_1_function"].ver)


def test_suggest_writes_nothing_and_shows_the_review_table(page_db, monkeypatch):
    before = _rows(page_db)
    at, fake = _open_and_suggest(monkeypatch, [GOOD])
    assert len(fake.calls) == 1
    assert _rows(page_db) == before
    assert at.session_state["tag_panel_1_open"]
    table = at.session_state["tag_proposals_1_function"].base
    assert list(table["Card"]) == ["Lightning Bolt", "Night's Whisper", "Sol Ring"]
    assert list(table["Accept"]) == [True, False, False]
    assert any("thin: Removal" in m.value for m in at.markdown)
    assert at.button(key="tag_panel_1_function_apply").label == "✅ Apply 1 row(s)"


def test_apply_writes_only_ticked_rows_then_undo_restores(page_db, monkeypatch):
    before = _rows(page_db)
    at, _ = _open_and_suggest(monkeypatch, [GOOD])
    _forget_board()
    at.button(key="tag_panel_1_function_apply").click()
    _run(at)
    assert _rows(page_db) == sorted(before + [("Lightning Bolt", "Ramp")])
    assert "Added 1 tag(s) on 1 card(s)" in [t.body for t in at.toast]
    assert not at.session_state["tag_panel_1_open"]
    # The unticked rows wait, and the tab says so.
    assert any("2 proposal(s) waiting" in c.value for c in at.caption)
    _forget_board()
    at.button(key="tag_panel_1_undo_btn").click()
    _run(at)
    assert _rows(page_db) == before
    assert not any(b.key == "tag_panel_1_undo_btn" for b in at.button)


def test_a_ticked_replace_row_through_the_table(page_db, monkeypatch):
    at, _ = _open_and_suggest(monkeypatch, [GOOD])
    at.session_state[_table_key(at)] = {"edited_rows": {0: {"Accept": False}, 2: {"Accept": True, "Replace": True}},
                                        "added_rows": [], "deleted_rows": []}
    # AppTest keeps injected editor state for one run, so the edit and the
    # click share it. The Apply button's own label comes from the run before.
    _forget_board()
    at.button(key="tag_panel_1_function_apply").click()
    _run(at)
    rows = _rows(page_db)
    assert ("Sol Ring", "Removal") in rows and ("Sol Ring", "Ramp") not in rows
    assert ("Lightning Bolt", "Ramp") not in rows


def test_close_keeps_ticks_and_reopening_spends_nothing(page_db, monkeypatch):
    at, fake = _open_and_suggest(monkeypatch, [GOOD])
    at.session_state[_table_key(at)] = {"edited_rows": {1: {"Accept": True}}, "added_rows": [], "deleted_rows": []}
    _forget_board()
    at.button(key="tag_panel_1_close").click()
    _run(at)
    assert any("3 proposal(s) waiting" in c.value for c in at.caption)
    _forget_board()
    at.button(key="tag_panel_1_open_btn").click()
    _run(at)
    assert list(at.session_state["tag_proposals_1_function"].base["Accept"]) == [True, True, False]
    assert len(fake.calls) == 1


def test_a_new_suggest_asks_first_and_keep_changes_nothing(page_db, monkeypatch):
    at, fake = _open_and_suggest(monkeypatch, [GOOD, GOOD])
    _forget_board()
    at.button(key="tag_panel_1_function_suggest").click()
    _run(at)
    assert len(fake.calls) == 1
    assert any("still unapplied" in w.value for w in at.warning)
    _forget_board()
    at.button(key="tag_panel_1_function_confirm_no").click()
    _run(at)
    assert len(fake.calls) == 1
    _forget_board()
    at.button(key="tag_panel_1_function_suggest").click()
    _run(at)
    _forget_board()
    at.button(key="tag_panel_1_function_confirm_yes").click()
    _run(at)
    assert len(fake.calls) == 2


def test_discard_clears_the_proposals(page_db, monkeypatch):
    before = _rows(page_db)
    at, _ = _open_and_suggest(monkeypatch, [GOOD])
    _forget_board()
    at.button(key="tag_panel_1_function_discard").click()
    _run(at)
    assert "tag_proposals_1_function" not in at.session_state
    assert _rows(page_db) == before


def test_a_failed_request_shows_a_retryable_error(page_db, monkeypatch):
    at, fake = _open_and_suggest(monkeypatch, [])     # no scripted reply: the call fails
    assert any("FakeProvider has no more" in e.value for e in at.error)
    assert "tag_proposals_1_function" not in at.session_state
    fake.replies.append(GOOD)
    _forget_board()
    at.button(key="tag_panel_1_function_retry").click()
    _run(at)
    assert not at.error
    assert len(at.session_state["tag_proposals_1_function"].rows) == 5


def test_a_card_left_out_can_be_retried_alone(page_db, monkeypatch):
    partial = {"cards": [r for r in GOOD["cards"] if r["card"] != "Mind Stone"]}
    # The first answer leaves a card out; so does its own retry.
    at, fake = _open_and_suggest(monkeypatch, [partial, {"cards": []}])
    assert any("1 card(s) were not reviewed: Mind Stone" in w.value for w in at.warning)
    fake.replies.append({"cards": [_answer("Mind Stone", ["Removal"], "Add {C}")]})
    _forget_board()
    at.button(key="tag_panel_1_function_retry").click()
    _run(at)
    p = at.session_state["tag_proposals_1_function"]
    assert p.missing == [] and "Mind Stone" in {r.row.card.name for r in p.rows}
    assert "Sol Ring" not in json.loads(json.dumps(fake.calls[-1]["schema"]))["properties"]["cards"]["items"][
        "properties"]["card"]["enum"]


def test_tick_and_drop_a_thin_label(page_db, monkeypatch):
    at, _ = _open_and_suggest(monkeypatch, [GOOD])
    _forget_board()
    at.button(key="tag_panel_1_function_tick_Removal").click()
    _run(at)
    assert "Night's Whisper" in {r.row.card.name for r in at.session_state["tag_proposals_1_function"].ticked()}
    _forget_board()
    at.button(key="tag_panel_1_function_drop_Removal").click()
    _run(at)
    p = at.session_state["tag_proposals_1_function"]
    assert not sqlite3.connect(page_db).execute(
        "SELECT 1 FROM deck_tag_vocab WHERE label='Removal'").fetchone()
    assert all("Removal" not in r.row.tags for r in p.rows)
    assert not any(b.key == "tag_panel_1_function_drop_Removal" for b in at.button)


def test_vocabulary_editor_drafts_then_saves_on_request(page_db, monkeypatch):
    conn = sqlite3.connect(page_db)
    writes.set_card_tags(conn, 1, "sol", "strategy", ["Rocks"])
    conn.close()
    fake = FakeProvider([{"definitions": [{"label": "Rocks", "definition": "Mana artifacts."}]}])
    monkeypatch.setattr(providers, "make_provider", lambda choice: fake)
    at = AppTest.from_file(EDITOR, default_timeout=20)
    at.session_state["current_deck_id"] = 1
    _run(at)
    at.selectbox(key="editor_1_cardtag_type_choice").set_value("strategy")
    _run(at)
    _forget_board()
    at.button(key="tag_vocab_1_strategy_draft").click()
    _run(at)
    saved = lambda: sqlite3.connect(page_db).execute(  # noqa: E731
        "SELECT label, definition FROM deck_tag_vocab WHERE tag_type='strategy'").fetchall()
    assert saved() == []       # shown, not stored
    _forget_board()
    at.button(key="tag_vocab_1_strategy_save").click()
    _run(at)
    assert saved() == [("Rocks", "Mana artifacts.")]


def test_a_layer_with_no_defined_labels_cannot_be_suggested(page_db, monkeypatch):
    conn = sqlite3.connect(page_db)
    writes.set_card_tags(conn, 1, "sol", "strategy", ["Rocks"])
    conn.close()
    at = AppTest.from_file(EDITOR, default_timeout=20)
    at.session_state["current_deck_id"] = 1
    _run(at)
    at.selectbox(key="editor_1_cardtag_type_choice").set_value("strategy")
    _run(at)
    _forget_board()
    at.button(key="tag_panel_1_open_btn").click()
    _run(at)
    assert not any(b.key == "tag_panel_1_strategy_suggest" for b in at.button)
    assert any("no defined labels" in i.value for i in at.info)


# ---- two-pass mode: propose a vocabulary ------------------------------------

def test_save_proposed_vocabulary_saves_only_kept_rows(conn, ids):
    edited = pd.DataFrame([
        {"Keep": True, "Label": "Ramp", "Definition": tag_vocab.FUNCTION_MENU["Ramp"]},   # the default: stored blank
        {"Keep": True, "Label": " Rocks ", "Definition": " Mana artifacts. "},
        {"Keep": False, "Label": "Skipped", "Definition": "No."},
        {"Keep": True, "Label": "", "Definition": "No label."},
    ])
    assert tag_panel.save_proposed_vocabulary(conn, 1, "function", edited) == 2
    assert q.active_tag_vocab(conn, 1, "function") == {"Ramp": "", "Rocks": "Mana artifacts."} or \
        q.active_tag_vocab(conn, 1, "function") == {"Ramp": None, "Rocks": "Mana artifacts."}


def tag_vocab_default(label):
    from dashboard_lib import tag_vocab
    return tag_vocab.FUNCTION_MENU[label]


def test_propose_then_edit_then_save_through_the_page(page_db, monkeypatch):
    conn = sqlite3.connect(page_db)
    conn.execute("DELETE FROM card_tags")
    conn.execute("DELETE FROM deck_tag_vocab")
    conn.commit()
    conn.close()
    fake = FakeProvider([{"labels": [
        {"label": "Ramp", "definition": "Mana."}, {"label": "Rocks", "definition": "Mana artifacts."}]}])
    monkeypatch.setattr(providers, "make_provider", lambda choice: fake)
    at = AppTest.from_file(EDITOR, default_timeout=20)
    at.session_state["current_deck_id"] = 1
    _run(at)
    at.selectbox(key="editor_1_cardtag_type_choice").set_value("function")
    _run(at)
    _forget_board()
    at.button(key="tag_vocab_1_function_propose").click()
    _run(at)
    stored = lambda: sqlite3.connect(page_db).execute(  # noqa: E731
        "SELECT label, definition FROM deck_tag_vocab ORDER BY label").fetchall()
    assert stored() == []      # shown, not stored
    assert "Ramp" in fake.calls[0]["messages"][0]["content"]
    _forget_board()
    at.button(key="tag_vocab_1_function_proposed_save").click()
    _run(at)
    assert stored() == [("Ramp", "Mana."), ("Rocks", "Mana artifacts.")]
    # pass two can now run: the layer has defined labels
    labels = {v["label"] for v in q.deck_tag_vocabulary(sqlite3.connect(page_db), 1, "function", defined_only=True)}
    assert labels == {"Ramp", "Rocks"}
