"""deck_board.board_payload() — the pure data contract behind the Stacks
view (VISUAL_DECK_PLAN.md Phase A), and apply_board_action(), the Phase B
edit handler. The JS side can't be run here, so the logic it relies on
(ordering, flags, URL-only images, what each edit writes) is pinned down on
the Python side."""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_lib import deck_board, writes  # noqa: E402


def _row(name, type_line="Creature", cmc=2, qty=1, **extra):
    row = {
        "scryfall_id": f"{name}-id", "name": name, "type_line": type_line,
        "mana_cost": "{1}{G}", "cmc": cmc, "color_identity": "G",
        "image_uri": f"https://cards.scryfall.io/{name}.jpg", "local_image_path": None,
        "is_basic_land": 0, "is_game_changer": 0, "is_reserved": 0,
        "current_price_usd": 1.5, "quantity": qty, "strategy_tags": None,
    }
    row.update(extra)
    return row


OWNED = {n.lower(): {"owned_qty": 1, "available_qty": 1} for n in ("Sol Ring", "Forest", "Birds", "Boss")}
META = {"commander": "Boss", "partner": None}


def test_empty_deck_gives_empty_payload():
    assert deck_board.board_payload(pd.DataFrame(), META, OWNED) == []


def test_commander_is_flagged_and_pinned_first():
    df = pd.DataFrame([_row("Birds", cmc=1), _row("Boss", "Legendary Creature", cmc=5), _row("Sol Ring", "Artifact", cmc=1)])
    cards = deck_board.board_payload(df, META, OWNED)
    assert [c["name"] for c in cards] == ["Boss", "Birds", "Sol Ring"]  # commander, then cmc, then name
    assert [c["flags"]["commander"] for c in cards] == [True, False, False]


def test_mdfc_land_bucketed_by_front_face_but_flagged_as_land():
    df = pd.DataFrame([_row("Boss"), _row("Bala Ged", "Sorcery // Land", cmc=0)])
    mdfc = next(c for c in deck_board.board_payload(df, META, OWNED) if c["name"] == "Bala Ged")
    assert mdfc["type"] == "Sorcery"
    assert mdfc["is_land"] is True


def test_not_owned_flag_skips_basics_and_uses_owned_map():
    df = pd.DataFrame([
        _row("Boss"), _row("Birds"), _row("Mystery Card"),
        _row("Plains", "Basic Land — Plains", cmc=0, is_basic_land=1),
    ])
    flags = {c["name"]: c["flags"]["not_owned"] for c in deck_board.board_payload(df, META, OWNED)}
    assert flags == {"Boss": False, "Birds": False, "Mystery Card": True, "Plains": False}


def test_tags_flags_and_nan_handling():
    df = pd.DataFrame([_row(
        "Boss", strategy_tags="Ramp,Removal", is_game_changer=1, is_reserved=1,
        current_price_usd=float("nan"), mana_cost=None,
    )])
    (card,) = deck_board.board_payload(df, META, OWNED)
    assert card["tags"] == ["Ramp", "Removal"]
    assert card["flags"]["game_changer"] and card["flags"]["reserved"]
    assert card["price"] is None
    assert card["mana_cost"] == ""


def test_payload_has_urls_only_and_stays_small():
    df = pd.DataFrame(
        [_row("Boss", "Legendary Creature", cmc=5)]
        + [_row(f"Card {i}", cmc=i % 7, strategy_tags="Ramp") for i in range(98)]
        + [_row("Forest", "Basic Land — Forest", cmc=0, qty=30, is_basic_land=1)]
    )
    cards = deck_board.board_payload(df, META, OWNED)
    blob = repr(cards)
    assert "data:" not in blob and "local_image_path" not in blob
    assert all(c["img"].startswith("http") for c in cards)
    assert next(c for c in cards if c["name"] == "Forest")["qty"] == 30
    assert deck_board.payload_size_bytes(cards) < 50_000


# ------------------------------------------------------------------
# Phase B: apply_board_action() — every op against the conftest DB, the
# history rows each one leaves, and that each returned `undo` (fed back
# through the same handler) restores the prior state.
# ------------------------------------------------------------------



@pytest.fixture
def deck(conn):
    cur = conn.execute("INSERT INTO decks (name) VALUES ('Gisa')")
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def ring(conn, deck, add_card):
    """Sol Ring on the mainboard at quantity 2, with no history rows yet
    (inserted directly, so the setup itself logs nothing)."""
    sid = add_card("Sol Ring")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,2)", (deck, sid))
    conn.commit()
    return sid


def _qty(conn, deck, sid):
    row = conn.execute("SELECT quantity FROM deck_cards WHERE deck_id=? AND scryfall_id=?", (deck, sid)).fetchone()
    return row[0] if row else None


def _history(conn, deck):
    return [
        (r["status"], r["add_scryfall_id"], r["remove_scryfall_id"], r["quantity"])
        for r in conn.execute(
            "SELECT * FROM deck_changes WHERE deck_id=? ORDER BY change_id", (deck,)
        ).fetchall()
    ]


def _tags(conn, deck, sid):
    return sorted(writes.get_card_tags_by_type(conn, deck, "strategy").get(sid, []))


def _act(conn, deck, **action):
    return deck_board.apply_board_action(conn, deck, action)


def test_inc_bumps_without_history_and_undo_decrements(conn, deck, ring):
    result = _act(conn, deck, op="inc", id=ring)
    assert _qty(conn, deck, ring) == 3
    assert result["message"] == "Sol Ring → ×3"
    assert _history(conn, deck) == []  # a quantity bump isn't an add or a removal
    deck_board.apply_board_action(conn, deck, result["undo"])
    assert _qty(conn, deck, ring) == 2


def test_dec_reads_the_database_so_repeats_dont_overwrite(conn, deck, ring):
    conn.execute("UPDATE deck_cards SET quantity=5 WHERE scryfall_id=?", (ring,))
    conn.commit()
    _act(conn, deck, op="dec", id=ring)
    _act(conn, deck, op="dec", id=ring)  # a double-click: two real decrements
    assert _qty(conn, deck, ring) == 3


def test_dec_from_one_removes_logs_and_undo_puts_it_back(conn, deck, ring):
    conn.execute("UPDATE deck_cards SET quantity=1 WHERE scryfall_id=?", (ring,))
    conn.commit()
    result = _act(conn, deck, op="dec", id=ring)
    assert _qty(conn, deck, ring) is None
    assert result["message"] == "Removed Sol Ring"
    assert result["undo"] == {"op": "set_qty", "id": ring, "qty": 1}
    assert _history(conn, deck) == [("applied", None, ring, 1)]

    deck_board.apply_board_action(conn, deck, result["undo"])
    assert _qty(conn, deck, ring) == 1
    # Undo doesn't erase the mistake: the re-add is its own applied row.
    assert _history(conn, deck) == [("applied", None, ring, 1), ("applied", ring, None, 1)]


def test_remove_logs_old_quantity_and_undo_restores_it_with_tags(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Ramp"])
    result = _act(conn, deck, op="remove", id=ring)
    assert _qty(conn, deck, ring) is None
    assert result["message"] == "Removed Sol Ring ×2"
    assert _history(conn, deck) == [("applied", None, ring, 2)]

    redo = deck_board.apply_board_action(conn, deck, result["undo"])
    assert _qty(conn, deck, ring) == 2
    assert _tags(conn, deck, ring) == ["Ramp"]  # card_tags outlive the deck_cards row
    assert redo["undo"] == {"op": "remove", "id": ring}


def test_to_shortlist_removes_and_files_an_idea_and_undo_reverses_both(conn, deck, ring):
    result = _act(conn, deck, op="to_shortlist", id=ring)
    assert _qty(conn, deck, ring) is None
    assert _history(conn, deck) == [("idea", ring, None, 2), ("applied", None, ring, 2)]
    assert result["message"] == "Moved Sol Ring ×2 to the Shortlist"

    deck_board.apply_board_action(conn, deck, result["undo"])
    assert _qty(conn, deck, ring) == 2
    idea = writes.get_change(conn, result["undo"]["change_id"])
    assert idea["status"] == "dropped"
    assert idea["notes"] is None  # dropped by the undo, not stamped by the auto-drop
    assert _history(conn, deck)[-1] == ("applied", ring, None, 2)
    assert writes.list_changes(conn, deck) == []  # nothing left open on the Shortlist


def test_to_shortlist_refuses_a_duplicate_idea_and_leaves_the_card(conn, deck, ring):
    writes.add_change(conn, deck, add_scryfall_id=ring, add_name="Sol Ring")
    with pytest.raises(ValueError, match="already on this deck's shortlist"):
        _act(conn, deck, op="to_shortlist", id=ring)
    assert _qty(conn, deck, ring) == 2


def test_set_tags_replaces_labels_and_undo_restores_old_ones(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Ramp"])
    result = _act(conn, deck, op="set_tags", id=ring, tags=["Ramp", "Combo"])
    assert _tags(conn, deck, ring) == ["Combo", "Ramp"]
    assert result["message"] == "Tagged Sol Ring: Ramp, Combo"
    assert _history(conn, deck) == []

    deck_board.apply_board_action(conn, deck, result["undo"])
    assert _tags(conn, deck, ring) == ["Ramp"]

    cleared = _act(conn, deck, op="set_tags", id=ring, tags=[])
    assert cleared["message"] == "Cleared tags on Sol Ring" and _tags(conn, deck, ring) == []


def test_unchanged_tags_are_a_noop_with_no_undo(conn, deck, ring):
    writes.set_card_tags(conn, deck, ring, "strategy", ["Ramp"])
    assert _act(conn, deck, op="set_tags", id=ring, tags=["ramp"])["undo"] is None


@pytest.mark.parametrize("action, match", [
    ("not a dict", "didn't come through"),
    ({"op": "explode", "id": "x"}, "Unknown board action"),
    ({"op": "inc"}, "which card"),
    ({"op": "inc", "id": "   "}, "which card"),
    ({"op": "set_tags", "id": "x"}, "list of tags"),
    ({"op": "set_tags", "id": "x", "tags": "Ramp"}, "list of tags"),
    ({"op": "set_qty", "id": "x", "qty": 0}, "invalid quantity"),
    ({"op": "set_qty", "id": "x", "qty": True}, "invalid quantity"),
    ({"op": "from_shortlist", "id": "x", "qty": 1}, "invalid Shortlist entry"),
])
def test_malformed_actions_raise(conn, deck, action, match):
    with pytest.raises(ValueError, match=match):
        deck_board.apply_board_action(conn, deck, action)


def test_card_no_longer_in_deck_raises_and_writes_nothing(conn, deck, add_card):
    sid = add_card("Lightning Greaves")
    for op in ("inc", "dec", "remove", "to_shortlist"):
        with pytest.raises(ValueError, match="isn't in this deck any more"):
            _act(conn, deck, op=op, id=sid)
    with pytest.raises(ValueError, match="card database"):
        _act(conn, deck, op="inc", id="no-such-card")
    assert _history(conn, deck) == []


# ------------------------------------------------------------------
# Phase B: stats strip
# ------------------------------------------------------------------
def test_board_stats_counts_lands_curve_and_average():
    df = pd.DataFrame([
        _row("Boss", "Legendary Creature", cmc=4),
        _row("Sol Ring", "Artifact", cmc=1),
        _row("Big", "Creature", cmc=9),
        _row("Forest", "Basic Land — Forest", cmc=0, qty=10),
        _row("Bala Ged", "Sorcery // Land", cmc=0),  # MDFC: a land, out of the curve
    ])
    stats = deck_board.board_stats(df)
    assert stats["total"] == 14 and stats["lands"] == 11 and stats["nonlands"] == 3
    assert stats["avg_mv"] == pytest.approx((4 + 1 + 9) / 3)
    assert stats["curve"] == [0, 1, 0, 0, 1, 0, 0, 1]  # 9 lands in the 7+ bucket


def test_board_stats_empty_deck():
    stats = deck_board.board_stats(pd.DataFrame())
    assert stats["total"] == 0 and stats["avg_mv"] is None and sum(stats["curve"]) == 0
