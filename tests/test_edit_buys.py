"""EDIT_FLOW_PLAN.md Phase 4: planned buys — a card can be on a deck's list
while you wait to buy it.

The world: Gisa, Prosper and Tuvasa (three decks) and a few cards. The rules
covered, in order:

  - a Buy add puts the card on the list and makes / moves NO lot (no phantom
    copy), on any build state, and History keeps `source='buy'`
  - Buy is allowed while you own copies elsewhere ("keep Gisa's, buy a second")
    and a bump of an existing card buys only the difference
  - the allocator: a Buy row is `buying`, claims nothing and is never in a
    conflict; every other row's verdicts are unchanged
  - the buy list's four reasons, one row per card under the most specific,
    quantities that don't double count, and the targets "Bought" acts on
  - "Bought" (record_purchase): a copy sleeved into the right deck, the card
    leaving the buy list, a Buy idea going back to Auto
  - order import matching (buy_matches / sleeve_purchases): one match, two
    decks wanting one copy, no match
  - the Board's "buying" badge and its source menu, History's words
"""
import os
import sqlite3

import pytest
import streamlit as st

from dashboard_lib import deck_board, formatting as fmt, moxfield_export as mox, queries as q, writes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RING, BLADE, LOTUS = "Sol Ring", "Heirloom Blade", "Jeweled Lotus"
SWAMP = "Swamp"


@pytest.fixture
def c():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    q.ensure_schema(conn)
    for place in ("Box", "Lands Box"):
        writes.add_location_to_catalog(conn, place)
    for name in (RING, BLADE, LOTUS):
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id,"
                     " current_price_usd) VALUES (?,?,'cmd','1',?,?)", (name, name, f"oracle-{name}", 2.0))
    conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id, is_basic_land)"
                 " VALUES (?,?,'cmd','2',?,1)", (SWAMP, SWAMP, f"oracle-{SWAMP}"))
    for deck in ("Gisa", "Prosper", "Tuvasa"):
        conn.execute("INSERT INTO decks (name) VALUES (?)", (deck,))
    conn.commit()
    yield conn
    conn.close()


def _deck(c, name):
    return c.execute("SELECT deck_id FROM decks WHERE name=?", (name,)).fetchone()[0]


def _lots(c, card=RING):
    return sorted((r[0] or "", r[1]) for r in c.execute(
        "SELECT location, quantity FROM collection WHERE scryfall_id=?", (card,)))


def _list(c, deck, card=RING):
    return q.deck_list_quantity(c, _deck(c, deck), card)


def _buy(c, deck, card=RING, qty=1):
    return writes.add_deck_card(c, _deck(c, deck), card, qty, source="buy")


def _row(rows, card):
    return next((r for r in rows if r["card"] == card), None)


# ------------------------------------------------------------------
# A Buy add: on the list, no lot
# ------------------------------------------------------------------
def test_a_buy_add_lists_the_card_and_makes_no_lot(c):
    moves = _buy(c, "Prosper")
    assert _list(c, "Prosper") == 1
    assert _lots(c) == []                                    # no phantom copy
    assert [(m["kind"], m["quantity"]) for m in moves] == [("buy", 1)]
    (row,) = c.execute("SELECT source, source_location, status FROM deck_changes").fetchall()
    assert tuple(row) == ("buy", None, "applied")
    assert c.execute("SELECT COUNT(*) FROM collection_moves").fetchone()[0] == 0


def test_buy_ignores_a_location_passed_with_it(c):
    assert writes.normalize_source(c, "buy", "Lands Box") == ("buy", None)


def test_buy_is_allowed_while_you_own_copies_elsewhere(c):
    """Keep Gisa's, buy a second: the owned copy stays where it is."""
    writes.add_collection_lot(c, RING, quantity=1, location="Gisa")
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (_deck(c, "Gisa"), RING))
    c.commit()
    _buy(c, "Prosper")
    assert _lots(c) == [("Gisa", 1)]
    assert _list(c, "Gisa") == 1 and _list(c, "Prosper") == 1


def test_buy_on_a_brewing_deck_still_says_buy_not_pull(c):
    writes.set_build_state(c, _deck(c, "Prosper"), "brewing")
    (move,) = _buy(c, "Prosper")
    assert move["kind"] == "buy"
    assert _lots(c) == []


def test_a_bump_buys_only_the_difference_and_a_basic_is_never_bought(c):
    writes.add_deck_card(c, _deck(c, "Prosper"), LOTUS, 1)               # Auto: a starter lot
    moves = writes.add_deck_card(c, _deck(c, "Prosper"), LOTUS, 3, source="buy")
    assert [(m["kind"], m["quantity"]) for m in moves] == [("buy", 2)]
    assert _lots(c, LOTUS) == [("Prosper", 1)]
    writes.add_collection_lot(c, SWAMP, quantity=None, location="Basics box")   # untracked bulk basic
    assert [m["kind"] for m in _buy(c, "Prosper", SWAMP, 10)] == ["bulk"]


def test_buying_what_is_already_sleeved_here_buys_nothing(c):
    writes.add_collection_lot(c, RING, quantity=1, location="Prosper")
    assert _buy(c, "Prosper") == []                       # the list needs 1, the sleeves hold 1
    assert q.awaiting_buys(c) == []


def test_undo_skips_the_buy_placeholder(c):
    moves = _buy(c, "Prosper")
    assert writes.revert_moves(c, moves) == ([], [])


def test_a_staged_buy_idea_applies_with_no_lot(c):
    cid = writes.add_change(c, _deck(c, "Prosper"), add_scryfall_id=LOTUS, add_name=LOTUS,
                            remove_scryfall_id=BLADE, remove_name=BLADE, status="planned", source="buy")
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (_deck(c, "Prosper"), BLADE))
    c.commit()
    out = writes.apply_change(c, cid)
    assert _lots(c, LOTUS) == [] and not out["created_lot"]
    assert [(m["kind"]) for m in out["moves"] if m["card"] == LOTUS] == ["buy"]
    assert c.execute("SELECT source, status FROM deck_changes WHERE change_id=?", (cid,)).fetchone()[:] == ("buy", "applied")
    assert [(b["deck_name"], b["card"]) for b in q.awaiting_buys(c)] == [("Prosper", LOTUS)]


def test_history_words_a_buy_route(c):
    assert fmt.format_change_route("buy", None, None) == "to buy"
    assert fmt.format_change_route("storage", "Lands Box", None) == "from Lands Box"


# ------------------------------------------------------------------
# The allocator
# ------------------------------------------------------------------
def test_a_buy_row_is_buying_and_claims_nothing(c):
    writes.add_collection_lot(c, RING, quantity=1, location="Box")
    g, p = _deck(c, "Gisa"), _deck(c, "Prosper")
    first = writes.add_change(c, g, add_scryfall_id=RING, add_name=RING, source="buy")
    second = writes.add_change(c, p, add_scryfall_id=RING, add_name=RING)
    verdicts = q.plan_availability(c, q.open_changes_dataframe(c, ("idea", "planned")))
    assert verdicts[first]["verdict"] == "buying" and verdicts[first]["claimed"] == 0
    # The other row still gets the one owned copy: the Buy row never took it.
    assert verdicts[second]["verdict"] == "available"
    assert "buying" in q.PLAN_VERDICTS


def test_a_buy_row_is_buying_even_with_a_copy_already_sleeved_here(c):
    writes.add_collection_lot(c, RING, quantity=1, location="Gisa")
    cid = writes.add_change(c, _deck(c, "Gisa"), add_scryfall_id=RING, add_name=RING, source="buy")
    assert q.plan_availability(c, q.open_changes_dataframe(c, ("idea",)))[cid]["verdict"] == "buying"


def test_buying_rows_are_not_a_conflict(c):
    writes.add_collection_lot(c, RING, quantity=1, location="Box")
    for deck in ("Gisa", "Prosper"):
        writes.add_change(c, _deck(c, deck), add_scryfall_id=RING, add_name=RING, source="buy")
    assert q.plan_conflicts(c, q.open_changes_dataframe(c, ("idea",))) == []


# ------------------------------------------------------------------
# The buy list
# ------------------------------------------------------------------
def test_each_of_the_four_reasons_lands_on_the_list(c):
    g, p, t = _deck(c, "Gisa"), _deck(c, "Prosper"), _deck(c, "Tuvasa")
    writes.add_change(c, g, add_scryfall_id=LOTUS, add_name=LOTUS)                         # idea, owned zero
    writes.add_collection_lot(c, RING, quantity=1, location="Box")
    writes.add_change(c, p, add_scryfall_id=RING, add_name=RING, source="buy")             # chosen, though owned
    _buy(c, "Tuvasa", BLADE)                                                               # awaiting
    rows = q.buy_list(c, contention=[{"oracle_id": "oracle-Contended", "card": "Contended", "shortfall": 2,
                                      "mainboard_decks": ["Gisa"], "shortlist_decks": []}])["rows"]
    assert {r["card"]: r["reason"] for r in rows} == {
        LOTUS: "idea", RING: "chosen", BLADE: "awaiting", "Contended": "contention"}
    assert _row(rows, RING)["decks"] == ["Prosper"]


def test_a_card_with_two_reasons_is_one_row_under_the_more_specific(c):
    """An idea you own none of AND marked Buy is `chosen`, once, quantity 1."""
    cid = writes.add_change(c, _deck(c, "Gisa"), add_scryfall_id=LOTUS, add_name=LOTUS, source="buy")
    rows = q.buy_list(c)["rows"]
    assert [(r["card"], r["reason"], r["quantity"]) for r in rows] == [(LOTUS, "chosen", 1)]
    # Awaiting beats chosen: Prosper already lists it from a Buy add.
    _buy(c, "Prosper", LOTUS)
    (row,) = q.buy_list(c)["rows"]
    assert row["reason"] == "awaiting" and row["quantity"] == 2          # two decks, two copies
    assert row["decks"] == ["Gisa", "Prosper"]
    assert {t["kind"] for t in row["targets"]} == {"awaiting", "chosen"}
    assert cid in {t["change_id"] for t in row["targets"]}


def test_a_contention_shortfall_adds_no_target_and_only_raises_a_smaller_total(c):
    _buy(c, "Prosper", LOTUS)
    cont = [{"oracle_id": f"oracle-{LOTUS}", "card": LOTUS, "shortfall": 3,
             "mainboard_decks": ["Prosper"], "shortlist_decks": []}]
    (row,) = q.buy_list(c, contention=cont)["rows"]
    assert (row["reason"], row["quantity"]) == ("awaiting", 3)           # the larger figure wins
    assert [t["kind"] for t in row["targets"]] == ["awaiting"]
    assert row["subtotal"] == pytest.approx(6.0)


def test_a_dismantled_deck_and_a_sleeved_copy_are_not_awaiting(c):
    _buy(c, "Prosper")
    _buy(c, "Gisa", LOTUS)
    writes.add_collection_lot(c, LOTUS, quantity=1, location="Gisa")
    writes.set_build_state(c, _deck(c, "Prosper"), "dismantled")
    assert q.awaiting_buys(c) == []


def test_the_text_export_names_the_new_reasons(c):
    _buy(c, "Prosper")
    assert "on a deck's list, no copy yet" in mox.export_buy_list_text(q.buy_list(c))


# ------------------------------------------------------------------
# "Bought"
# ------------------------------------------------------------------
def test_bought_sleeves_a_new_lot_into_the_right_deck_and_clears_awaiting(c):
    _buy(c, "Prosper")
    _buy(c, "Gisa")
    (row,) = q.buy_list(c)["rows"]
    target = next(t for t in row["targets"] if t["deck_name"] == "Prosper")
    moves = writes.record_purchase(c, target["deck_id"], target["scryfall_id"], 1, price_paid=1.5)
    assert [(m["kind"], m["to"]) for m in moves] == [("new", "Prosper")]
    assert _lots(c) == [("Prosper", 1)]
    assert c.execute("SELECT price_paid, source FROM collection").fetchone()[:] == (1.5, writes.PURCHASE_LOT_SOURCE)
    assert c.execute("SELECT reason, to_location FROM collection_moves").fetchone()[:] == ("purchase", "Prosper")
    (left,) = q.buy_list(c)["rows"]
    assert [t["deck_name"] for t in left["targets"]] == ["Gisa"] and left["quantity"] == 1
    assert "purchase" in fmt.MOVE_REASON_LABELS


def test_bought_for_a_buy_idea_sends_it_back_to_auto_so_apply_finds_the_copy(c):
    cid = writes.add_change(c, _deck(c, "Gisa"), add_scryfall_id=LOTUS, add_name=LOTUS, source="buy")
    (row,) = q.buy_list(c)["rows"]
    (t,) = row["targets"]
    writes.record_purchase(c, t["deck_id"], t["scryfall_id"], 1, change_id=t["change_id"])
    assert c.execute("SELECT source FROM deck_changes WHERE change_id=?", (cid,)).fetchone()[0] is None
    assert q.buy_list(c)["rows"] == []                      # off the list: the idea is Auto and owned now
    # Adding it later finds the sleeved copy and makes no second lot.
    writes.add_deck_card(c, _deck(c, "Gisa"), LOTUS, 1)
    assert _lots(c, LOTUS) == [("Gisa", 1)]


def test_a_part_purchase_leaves_a_buy_idea_marked(c):
    cid = writes.add_change(c, _deck(c, "Gisa"), add_scryfall_id=LOTUS, add_name=LOTUS, source="buy", quantity=2)
    writes.record_purchase(c, _deck(c, "Gisa"), LOTUS, 1, change_id=cid)
    assert c.execute("SELECT source FROM deck_changes WHERE change_id=?", (cid,)).fetchone()[0] == "buy"


def test_bought_refuses_a_dismantled_deck_a_basic_and_zero(c):
    writes.set_build_state(c, _deck(c, "Gisa"), "dismantled")
    writes.add_collection_lot(c, SWAMP, quantity=None, location="Basics box")
    for deck, card, qty, match in (("Gisa", RING, 1, "dismantled"), ("Prosper", SWAMP, 1, "untracked"),
                                   ("Prosper", RING, 0, "at least 1")):
        with pytest.raises(ValueError, match=match):
            writes.record_purchase(c, _deck(c, deck), card, qty)
    assert _lots(c) == []


# ------------------------------------------------------------------
# Order import
# ------------------------------------------------------------------
def _imported(c, card=RING, qty=1, location=None):
    lot = writes.add_collection_lot(c, card, quantity=qty, location=location, reason="import")
    return {"collection_id": lot, "scryfall_id": card, "quantity": qty, "location": location}


def test_an_imported_lot_matches_the_deck_waiting_on_it(c):
    _buy(c, "Prosper")
    (offer,) = q.buy_matches(c, [_imported(c)])
    assert (offer["deck_name"], offer["quantity"], offer["kind"], offer["default"]) == ("Prosper", 1, "awaiting", True)
    lines, moves = writes.sleeve_purchases(c, [offer])
    assert _lots(c) == [("Prosper", 1)]
    assert [(m["from"], m["to"]) for m in moves] == [(None, "Prosper")] and "sleeved" in lines[0]
    assert c.execute("SELECT reason FROM collection_moves ORDER BY move_id DESC LIMIT 1").fetchone()[0] == "purchase"
    assert q.awaiting_buys(c) == []


def test_two_decks_wanting_one_copy_both_get_an_offer_only_the_first_ticked(c):
    _buy(c, "Gisa")
    _buy(c, "Prosper")
    first, second = q.buy_matches(c, [_imported(c)])
    assert (first["default"], second["default"]) == (True, False)
    # Ticking both anyway can't hand out a copy twice: the second is reported.
    lines, moves = writes.sleeve_purchases(c, [first, second])
    assert len(moves) == 1 and "no copies left" in lines[1]
    assert _lots(c) == [(first["deck_name"], 1)]


def test_a_lot_with_no_matching_buy_gets_no_offer_and_neither_does_the_deck_it_is_already_in(c):
    assert q.buy_matches(c, [_imported(c, LOTUS)]) == []
    _buy(c, "Prosper", LOTUS)
    assert q.buy_matches(c, [_imported(c, LOTUS, location="Prosper")]) == []


def test_a_buy_idea_matches_too_and_goes_back_to_auto(c):
    cid = writes.add_change(c, _deck(c, "Tuvasa"), add_scryfall_id=RING, add_name=RING, source="buy")
    (offer,) = q.buy_matches(c, [_imported(c)])
    assert (offer["kind"], offer["change_id"]) == ("chosen", cid)
    writes.sleeve_purchases(c, [offer])
    assert c.execute("SELECT source FROM deck_changes WHERE change_id=?", (cid,)).fetchone()[0] is None


# ------------------------------------------------------------------
# The Board
# ------------------------------------------------------------------
def test_the_board_marks_a_buying_card_and_not_as_not_owned(c):
    _buy(c, "Prosper")
    g = _deck(c, "Prosper")
    assert q.deck_buying(c, g) == {RING: 1}
    df = q.deck_cards_dataframe(c, g)
    (card,) = deck_board.board_payload(df, {}, {}, buying=q.deck_buying(c, g))
    assert card["flags"]["buying"] and not card["flags"]["not_owned"]
    (plain,) = deck_board.board_payload(df, {}, {})
    assert plain["flags"]["not_owned"] and not plain["flags"]["buying"]


def test_the_shortlist_payload_offers_buy_and_badges_a_buy_row(c):
    # load_board_shortlist() reads through st.cache_data, keyed by deck id and
    # not by connection: clear it so this world can't leak into another test's.
    st.cache_data.clear()
    cid = writes.add_change(c, _deck(c, "Gisa"), add_scryfall_id=RING, add_name=RING, source="buy")
    (item,) = deck_board.load_board_shortlist(c, _deck(c, "Gisa"))
    st.cache_data.clear()
    assert (item["source"], item["can_buy"], item["badge"], item["verdict"]) == ("buy", True, "buying", "buying")
    assert deck_board.VERDICT_BADGES["buying"] == "buying" and item["change_id"] == cid


def test_board_add_and_apply_accept_buy(c):
    g = _deck(c, "Gisa")
    idea = writes.add_change(c, g, add_scryfall_id=RING, add_name=RING)
    result = deck_board.apply_board_action(c, g, {"op": "add", "change_id": idea, "source": "buy"})
    assert "to buy" in result["message"] and _lots(c) == [] and _list(c, "Gisa") == 1
    assert [b["card"] for b in q.awaiting_buys(c)] == [RING]
    # Undo: back to the Shortlist, nothing to put back.
    deck_board.apply_board_action(c, g, result["undo"], undo_moves=result["moves"])
    assert _list(c, "Gisa") == 0


def test_a_bad_source_from_the_browser_is_still_refused(c):
    g = _deck(c, "Gisa")
    idea = writes.add_change(c, g, add_scryfall_id=RING, add_name=RING)
    with pytest.raises(ValueError, match="isn't a choice"):
        deck_board.apply_board_action(c, g, {"op": "add", "change_id": idea, "source": "steal"})
