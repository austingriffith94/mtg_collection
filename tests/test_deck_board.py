"""deck_board.board_payload() — the pure data contract behind the Stacks
view (VISUAL_DECK_PLAN.md Phase A), and apply_board_action(), the Phase B
edit handler. The JS side can't be run here, so the logic it relies on
(ordering, flags, URL-only images, what each edit writes) is pinned down on
the Python side."""
import os
import sqlite3
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_lib import deck_board, queries as q, writes  # noqa: E402


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


def test_inc_bumps_log_a_quantity_change_not_an_add_and_undo_decrements(conn, deck, ring):
    result = _act(conn, deck, op="inc", id=ring)
    assert _qty(conn, deck, ring) == 3
    assert result["message"] == "Sol Ring → ×3"
    # Phase 0: the bump is in history (quantity 2 -> 3), but as a diff of 1,
    # not a fresh "add" of the whole new count.
    assert _history(conn, deck) == [("applied", ring, None, 1)]
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


# ------------------------------------------------------------------
# Phase C: the Shortlist column — payload, cross-deck verdicts, and the
# plan / unpair / apply ops.
# ------------------------------------------------------------------
def _sl_row(name, change_id, status="idea", replace_target=None, replace_id=None, **extra):
    row = _row(name)
    row.update(change_id=change_id, status=status, replace_target=replace_target,
               replace_id=replace_id, notes=None)
    row.update(extra)
    return row


def test_shortlist_payload_orders_ideas_then_planned_and_maps_badges():
    df = pd.DataFrame([
        _sl_row("Zebra", 1, "planned", replace_target="Birds", replace_id="Birds-id"),
        _sl_row("Apple", 2, "planned", replace_target="Birds", replace_id="Birds-id"),
        _sl_row("Mango", 3, "idea", replace_target="Boss", replace_id="Boss-id"),
        _sl_row("Kiwi", 4, "idea"),
    ])
    availability = {
        1: {"verdict": "available", "text": "In storage: Box"},
        2: {"verdict": "not_owned", "text": "Not owned — buy"},
        3: {"verdict": "claimed_by", "text": "Claimed first by Twin (#9)"},
    }
    items = deck_board.shortlist_payload(df, availability)
    assert [(i["name"], i["status"]) for i in items] == [
        ("Kiwi", "idea"), ("Mango", "idea"), ("Apple", "planned"), ("Zebra", "planned"),
    ]
    by_name = {i["name"]: i for i in items}
    assert by_name["Zebra"]["badge"] == "available" and by_name["Zebra"]["verdict_text"] == "In storage: Box"
    assert by_name["Apple"]["badge"] == "buy"
    assert by_name["Mango"]["badge"] == "elsewhere"
    assert by_name["Kiwi"]["badge"] == "neutral" and by_name["Kiwi"]["verdict_text"] == ""
    assert "local_image_path" not in repr(items)

    # Only PLANNED rows mark the board; an idea's target is a suggestion.
    cuts = deck_board.planned_cuts(items)
    assert cuts == {"Birds-id": ["Apple", "Zebra"]}
    board = deck_board.board_payload(pd.DataFrame([_row("Boss"), _row("Birds")]), META, OWNED, cut_for=cuts)
    assert {c["name"]: c["cut_for"] for c in board} == {"Boss": [], "Birds": ["Apple", "Zebra"]}


def test_empty_shortlist_payload():
    assert deck_board.shortlist_payload(pd.DataFrame(), {}) == []
    assert deck_board.planned_cuts([]) == {}


def _open_df(conn):
    return q.open_changes_dataframe(conn, ("idea", "planned"))


def test_cross_deck_collision_shows_on_this_decks_shortlist_card(conn, deck, add_card):
    """You own one Arcane Signet. Twin planned it first; Gisa's idea for it
    must say so — a one-deck allocation would call it available."""
    signet = add_card("Arcane Signet")
    writes.add_collection_lot(conn, signet, quantity=1, location="Box")
    twin = conn.execute("INSERT INTO decks (name) VALUES ('Twin')").lastrowid
    blade = add_card("Heirloom Blade")
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (twin, blade))
    conn.commit()
    writes.add_change(conn, twin, add_scryfall_id=signet, remove_scryfall_id=blade, status="planned")
    mine = writes.add_change(conn, deck, add_scryfall_id=signet)

    items = deck_board.shortlist_payload(q.shortlist_dataframe(conn, deck),
                                         deck_board.shortlist_availability(conn, _open_df(conn)))
    (item,) = items
    assert item["change_id"] == mine
    assert item["verdict"] == "claimed_by" and item["badge"] == "elsewhere"
    assert "Twin" in item["verdict_text"]

    # The same row allocated over this deck alone looks fine — the reason
    # the Board allocates over every deck.
    alone = q.plan_availability(conn, q.open_changes_dataframe(conn, ("idea", "planned"), deck_id=deck))
    assert alone[mine]["verdict"] == "available"


def test_shortlist_availability_of_nothing_is_empty(conn):
    assert deck_board.shortlist_availability(conn, pd.DataFrame()) == {}


@pytest.fixture
def idea(conn, deck, ring, add_card):
    """An idea to bring in Arcane Signet (one copy in the Box); Sol Ring
    (the `ring` fixture) is on the mainboard to be cut."""
    signet = add_card("Arcane Signet")
    writes.add_collection_lot(conn, signet, quantity=1, location="Box")
    return {"change": writes.add_change(conn, deck, add_scryfall_id=signet, add_name="Arcane Signet"),
            "signet": signet}


def test_plan_pairs_the_idea_and_undo_unpairs_it(conn, deck, ring, idea):
    result = _act(conn, deck, op="plan", change_id=idea["change"], cut_id=ring)
    row = writes.get_change(conn, idea["change"])
    assert row["status"] == "planned" and row["remove_scryfall_id"] == ring
    assert result["message"] == "Planned: Arcane Signet in for Sol Ring"
    assert result["undo"] == {"op": "unpair", "change_id": idea["change"]}

    deck_board.apply_board_action(conn, deck, result["undo"])
    row = writes.get_change(conn, idea["change"])
    assert row["status"] == "idea" and row["remove_scryfall_id"] == ring  # pairing kept
    assert _qty(conn, deck, ring) == 2   # planning never touches the mainboard


def test_unpair_keeps_the_pairing_and_undo_replans_it(conn, deck, ring, idea):
    _act(conn, deck, op="plan", change_id=idea["change"], cut_id=ring)
    result = _act(conn, deck, op="unpair", change_id=idea["change"])
    assert writes.get_change(conn, idea["change"])["status"] == "idea"
    assert result["undo"] == {"op": "plan", "change_id": idea["change"], "cut_id": ring}
    deck_board.apply_board_action(conn, deck, result["undo"])
    assert writes.get_change(conn, idea["change"])["status"] == "planned"


@pytest.mark.parametrize("make_action, match", [
    (lambda s: {"op": "plan", "change_id": s["change"], "cut_id": "Lightning Greaves-cmd-1"}, "isn't in this deck"),
    (lambda s: {"op": "plan", "change_id": s["change"], "cut_id": s["signet"]}, "isn't in this deck"),
    (lambda s: {"op": "unpair", "change_id": s["change"]}, "is an idea"),
    (lambda s: {"op": "apply", "change_id": s["change"]}, "is an idea"),
    (lambda s: {"op": "plan", "change_id": 9999, "cut_id": "x"}, "isn't on this deck"),
])
def test_change_ops_refuse_stale_or_wrong_rows(conn, deck, ring, idea, add_card, make_action, match):
    add_card("Lightning Greaves")
    before = writes.get_change(conn, idea["change"])
    with pytest.raises(ValueError, match=match):
        deck_board.apply_board_action(conn, deck, make_action(idea))
    assert writes.get_change(conn, idea["change"]) == before


def test_plan_refuses_a_card_replacing_itself(conn, deck, ring):
    change = writes.add_change(conn, deck, add_scryfall_id=ring, add_name="Sol Ring")
    with pytest.raises(ValueError, match="can't replace itself"):
        _act(conn, deck, op="plan", change_id=change, cut_id=ring)


def test_change_ops_refuse_another_decks_row(conn, deck, ring, idea):
    other = conn.execute("INSERT INTO decks (name) VALUES ('Twin')").lastrowid
    conn.commit()
    with pytest.raises(ValueError, match="isn't on this deck"):
        deck_board.apply_board_action(conn, other, {"op": "unpair", "change_id": idea["change"]})


def test_planning_an_already_planned_row_says_so(conn, deck, ring, idea):
    _act(conn, deck, op="plan", change_id=idea["change"], cut_id=ring)
    with pytest.raises(ValueError, match="already planned"):
        _act(conn, deck, op="plan", change_id=idea["change"], cut_id=ring)


@pytest.mark.parametrize("action, match", [
    ({"op": "plan", "change_id": 1}, "which card to cut"),
    ({"op": "plan", "change_id": 1, "cut_id": "  "}, "which card to cut"),
    ({"op": "apply"}, "invalid Shortlist entry"),
    ({"op": "unpair", "change_id": 0}, "invalid Shortlist entry"),
])
def test_malformed_change_actions_raise(conn, deck, action, match):
    with pytest.raises(ValueError, match=match):
        deck_board.apply_board_action(conn, deck, action)


# Apply parity: the board's `apply` must leave the database exactly as the
# Shortlist tab's writes.apply_changes() does. Same scenario on two fresh
# databases, one applied each way, then compared table by table.
def _fresh_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    q.ensure_schema(c)
    return c


def _swap_scenario(c, *, owned):
    """Gisa runs Heirloom Blade (sleeved); a planned swap brings in Sol Ring,
    which is in the Box when `owned`, else not owned at all."""
    for name in ("Heirloom Blade", "Sol Ring"):
        c.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id)"
                  " VALUES (?,?,'cmd','1',?)",
                  (name, name, f"oracle-{name}"))
    deck_id = c.execute("INSERT INTO decks (name) VALUES ('Gisa')").lastrowid
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?, 'Heirloom Blade', 1)", (deck_id,))
    c.commit()
    writes.add_collection_lot(c, "Heirloom Blade", quantity=1, location="Gisa")
    if owned:
        writes.add_collection_lot(c, "Sol Ring", quantity=1, location="Box")
    change_id = writes.add_change(c, deck_id, add_scryfall_id="Sol Ring")
    deck_board.apply_board_action(c, deck_id, {"op": "plan", "change_id": change_id, "cut_id": "Heirloom Blade"})
    return deck_id, change_id


def _state(c):
    def dump(sql):
        return [tuple(r) for r in c.execute(sql).fetchall()]
    return {
        "deck_cards": dump("SELECT deck_id, scryfall_id, quantity FROM deck_cards ORDER BY scryfall_id"),
        "changes": dump("""SELECT change_id, status, add_scryfall_id, add_name, remove_scryfall_id,
                                  remove_name, quantity, resolved_at IS NOT NULL
                           FROM deck_changes ORDER BY change_id"""),
        "collection": dump("SELECT scryfall_id, quantity, location FROM collection ORDER BY collection_id"),
    }


@pytest.mark.parametrize("owned", [True, False])
def test_apply_matches_the_shortlist_tabs_apply_path(owned):
    via_board, via_tab = _fresh_conn(), _fresh_conn()
    try:
        deck_id, change_id = _swap_scenario(via_board, owned=owned)
        result = deck_board.apply_board_action(via_board, deck_id, {"op": "apply", "change_id": change_id})
        _, change_id2 = _swap_scenario(via_tab, owned=owned)
        (tab_result,) = writes.apply_changes(via_tab, [change_id2])
        assert tab_result["ok"]

        assert _state(via_board) == _state(via_tab)
        assert result["undo"] is None
        assert result["message"].startswith("Applied: Sol Ring in, Heirloom Blade out")
        assert ("starter collection lot" in result["message"]) == (not owned)
        # And it really applied: Sol Ring in and sleeved here.
        assert _state(via_board)["deck_cards"] == [(deck_id, "Sol Ring", 1)]
        assert ("Sol Ring", 1, "Gisa") in _state(via_board)["collection"]
    finally:
        via_board.close()
        via_tab.close()


def test_apply_refusal_surfaces_as_value_error_and_writes_nothing(conn, deck, ring, idea):
    _act(conn, deck, op="plan", change_id=idea["change"], cut_id=ring)
    writes.remove_deck_card(conn, deck, ring)   # the cut left the deck since the board rendered
    with pytest.raises(ValueError, match="Couldn't apply Arcane Signet"):
        _act(conn, deck, op="apply", change_id=idea["change"])
    assert writes.get_change(conn, idea["change"])["status"] == "planned"


# Name-only rows: every swap backfilled from the old queue stores a typed
# add name and no printing. shortlist_dataframe() drops them (INNER join),
# which left the Board's Planned section empty on the real database.
@pytest.fixture
def unlinked_swap(conn, deck, ring, add_card):
    """A planned swap Arcane Signet (typed name only) in for Sol Ring."""
    add_card("Arcane Signet")
    cur = conn.execute(
        """INSERT INTO deck_changes (deck_id, status, add_name, remove_scryfall_id, remove_name,
                                     quantity, planned_at)
           VALUES (?, 'planned', 'Arcane Signet', ?, 'Sol Ring', 1, CURRENT_TIMESTAMP)""",
        (deck, ring),
    )
    conn.commit()
    return cur.lastrowid


def test_unlinked_planned_swap_is_on_the_shortlist_and_marks_its_cut(conn, deck, ring, unlinked_swap):
    open_df = _open_df(conn)
    unlinked = open_df[(open_df["deck_id"] == deck) & open_df["add_scryfall_id"].isna()]
    items = deck_board.shortlist_payload(q.shortlist_dataframe(conn, deck),
                                         deck_board.shortlist_availability(conn, open_df), unlinked)
    (item,) = items
    assert item["change_id"] == unlinked_swap and item["unlinked"] is True
    assert item["status"] == "planned" and item["name"] == "Arcane Signet"
    assert item["id"] is None and item["img"] is None
    assert item["replaces"] == "Sol Ring" and item["badge"] == "buy"   # no copy owned
    assert deck_board.planned_cuts(items) == {ring: ["Arcane Signet"]}


def test_apply_links_an_unlinked_row_through_the_resolver(conn, deck, ring, unlinked_swap):
    signet = "Arcane Signet-cmd-1"
    with pytest.raises(ValueError, match="isn't linked to a printing"):
        _act(conn, deck, op="apply", change_id=unlinked_swap)   # no resolver: refused
    asked = []
    result = deck_board.apply_board_action(
        conn, deck, {"op": "apply", "change_id": unlinked_swap},
        resolve_add=lambda name: (asked.append(name), (signet, False, None))[1],
    )
    assert asked == ["Arcane Signet"]
    row = writes.get_change(conn, unlinked_swap)
    assert row["status"] == "applied" and row["add_scryfall_id"] == signet
    assert _qty(conn, deck, signet) == 1 and _qty(conn, deck, ring) is None
    assert result["message"].startswith("Applied: Arcane Signet in, Sol Ring out")


# ------------------------------------------------------------------
# Phase C: drop / add (no cut) / reopen
# ------------------------------------------------------------------
@pytest.mark.parametrize("planned", [False, True])
def test_drop_keeps_the_row_and_undo_reopens_it(conn, deck, ring, idea, planned):
    if planned:
        _act(conn, deck, op="plan", change_id=idea["change"], cut_id=ring)
    result = _act(conn, deck, op="drop", change_id=idea["change"])
    assert writes.get_change(conn, idea["change"])["status"] == "dropped"   # kept, never deleted
    assert result["undo"] == {"op": "reopen", "change_id": idea["change"]}
    assert _qty(conn, deck, ring) == 2   # the board is untouched

    back = deck_board.apply_board_action(conn, deck, result["undo"])
    assert writes.get_change(conn, idea["change"])["status"] == "idea"
    assert back["undo"] == {"op": "drop", "change_id": idea["change"]}


def test_drop_and_reopen_refuse_stale_rows(conn, deck, ring, idea):
    with pytest.raises(ValueError, match="already dropped|is an idea"):
        _act(conn, deck, op="reopen", change_id=idea["change"])   # an idea can't be reopened
    _act(conn, deck, op="drop", change_id=idea["change"])
    with pytest.raises(ValueError, match="already dropped"):
        _act(conn, deck, op="drop", change_id=idea["change"])


def test_add_puts_the_card_in_without_a_cut_and_retires_the_idea(conn, deck, ring, idea):
    result = _act(conn, deck, op="add", change_id=idea["change"])
    assert _qty(conn, deck, idea["signet"]) == 1
    assert _qty(conn, deck, ring) == 2                          # nothing cut
    assert writes.get_change(conn, idea["change"])["status"] == "dropped"   # idea retired, not applied
    assert _history(conn, deck)[-1] == ("applied", idea["signet"], None, 1)  # one add row
    assert result["message"] == "Added Arcane Signet to the deck"

    deck_board.apply_board_action(conn, deck, result["undo"])   # undo: back on the Shortlist
    assert _qty(conn, deck, idea["signet"]) is None
    assert [r["status"] for r in conn.execute(
        "SELECT status FROM deck_changes WHERE add_scryfall_id=? AND status IN ('idea','planned')",
        (idea["signet"],)).fetchall()] == ["idea"]


def test_add_refuses_a_planned_row_and_a_card_already_in_the_deck(conn, deck, ring, idea):
    _act(conn, deck, op="plan", change_id=idea["change"], cut_id=ring)
    with pytest.raises(ValueError, match="already planned"):
        _act(conn, deck, op="add", change_id=idea["change"])
    _act(conn, deck, op="unpair", change_id=idea["change"])
    writes.add_deck_card(conn, deck, idea["signet"], 1)       # added by hand meanwhile
    conn.execute("UPDATE deck_changes SET status='idea' WHERE change_id=?", (idea["change"],))
    conn.commit()
    with pytest.raises(ValueError, match="already in the deck"):
        _act(conn, deck, op="add", change_id=idea["change"])
    assert _qty(conn, deck, idea["signet"]) == 1               # not bumped to 2


def test_add_links_a_typed_name_idea_through_the_resolver_and_retires_it(conn, deck, ring, add_card):
    signet = add_card("Arcane Signet")
    change = conn.execute(
        "INSERT INTO deck_changes (deck_id, status, add_name, quantity) VALUES (?, 'idea', 'Arcane Signet', 1)",
        (deck,)).lastrowid
    conn.commit()
    with pytest.raises(ValueError, match="isn't linked to a printing"):
        _act(conn, deck, op="add", change_id=change)           # no resolver: refused, nothing written
    assert _qty(conn, deck, signet) is None
    deck_board.apply_board_action(conn, deck, {"op": "add", "change_id": change},
                                  resolve_add=lambda name: (signet, False, None))
    assert _qty(conn, deck, signet) == 1
    assert writes.get_change(conn, change)["status"] == "dropped"   # add_deck_card can't match a typed name; the handler does
    with pytest.raises(ValueError, match="Couldn't find"):
        c2 = conn.execute("INSERT INTO deck_changes (deck_id, status, add_name) VALUES (?, 'idea', 'Nope')", (deck,)).lastrowid
        conn.commit()
        deck_board.apply_board_action(conn, deck, {"op": "add", "change_id": c2},
                                      resolve_add=lambda name: (None, False, "not found"))
