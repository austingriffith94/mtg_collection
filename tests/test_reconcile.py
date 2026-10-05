"""Reconciliation — Phase 4 of the Workbench rework (see
IMPLEMENTATION_PLAN.md): unlocated lots, the per-deck list-vs-physical diff
in both directions, per-deck build status, and splitting a lot when only
some of its copies move.
"""
from dashboard_lib import queries as q
from dashboard_lib import writes


def _deck(conn, name):
    cur = conn.execute("INSERT INTO decks (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def _lot(conn, sid, location=None, quantity=1, price_paid=None):
    cur = conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, location, price_paid) VALUES (?,?,?,?)",
        (sid, quantity, location, price_paid),
    )
    conn.commit()
    return cur.lastrowid


def _main(conn, deck_id, sid, quantity=1):
    conn.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)",
                 (deck_id, sid, quantity))
    conn.commit()


def _basic(conn, add_card, name="Swamp"):
    sid = add_card(name)
    conn.execute("UPDATE cards SET is_basic_land=1 WHERE scryfall_id=?", (sid,))
    conn.commit()
    return sid


def _diff(conn, deck_id):
    return q.deck_reconciliation(conn, deck_id)[0]


def test_fully_located_deck_reports_zero_both_ways(conn, add_card):
    d = _deck(conn, "Raktres")
    for name in ("Sol Ring", "Blood Crypt"):
        sid = add_card(name)
        _main(conn, d, sid)
        _lot(conn, sid, location="Raktres")
    r = _diff(conn, d)
    assert r["to_pull"] == [] and r["to_put_away"] == []
    assert r["build"] == {"needed": 2, "located": 2, "pct": 100.0}


def test_mainboard_card_with_no_lot_here_is_to_pull(conn, add_card):
    d = _deck(conn, "Raktres")
    sid = add_card("Sol Ring")
    _main(conn, d, sid)
    lot = _lot(conn, sid, location="Box")
    r = _diff(conn, d)
    assert [(p["card"], p["short"]) for p in r["to_pull"]] == [("Sol Ring", 1)]
    assert [c["collection_id"] for c in r["to_pull"][0]["candidates"]] == [lot]


def test_lot_here_but_not_in_mainboard_is_to_put_away(conn, add_card):
    d = _deck(conn, "Raktres")
    sid = add_card("Heirloom Blade")
    lot = _lot(conn, sid, location="Raktres")
    r = _diff(conn, d)
    assert [(p["card"], p["surplus"]) for p in r["to_put_away"]] == [("Heirloom Blade", 1)]
    assert r["to_put_away"][0]["lots"][0]["collection_id"] == lot
    assert r["to_pull"] == []


def test_basics_never_appear_in_either_direction(conn, add_card):
    d = _deck(conn, "Raktres")
    swamp = _basic(conn, add_card)
    _main(conn, d, swamp, quantity=10)
    _lot(conn, swamp, location="Raktres", quantity=4)  # stray basic lot here
    r = _diff(conn, d)
    assert r["to_pull"] == [] and r["to_put_away"] == []
    assert r["build"]["pct"] is None  # nothing non-basic to build


def test_build_status_full_vs_paper_only(conn, add_card):
    built, paper = _deck(conn, "Built"), _deck(conn, "Paper")
    for i in range(4):
        sid = add_card(f"Card {i}")
        _main(conn, built, sid)
        _lot(conn, sid, location="Built")
        _main(conn, paper, sid)  # same cards, but their lots are in Built
    assert _diff(conn, built)["build"]["pct"] == 100.0
    assert _diff(conn, paper)["build"]["pct"] == 0.0


def test_other_printing_located_here_counts(conn, add_card):
    d = _deck(conn, "Raktres")
    on_list = add_card("Sol Ring", set_code="cmd")
    sleeved = add_card("Sol Ring", set_code="c21")
    _main(conn, d, on_list)
    _lot(conn, sleeved, location="Raktres")
    assert _diff(conn, d)["to_pull"] == []


def test_surplus_does_not_mask_shortfall_in_build_pct(conn, add_card):
    d = _deck(conn, "Raktres")
    a, b = add_card("Card A"), add_card("Card B")
    _main(conn, d, a)
    _main(conn, d, b)
    _lot(conn, a, location="Raktres", quantity=3)
    assert _diff(conn, d)["build"] == {"needed": 2, "located": 1, "pct": 50.0}


def test_candidates_prefer_storage_over_another_deck(conn, add_card):
    d, other = _deck(conn, "Raktres"), _deck(conn, "Marchesa")
    sid = add_card("Sol Ring")
    _main(conn, d, sid)
    raided = _lot(conn, sid, location="Marchesa")
    free = _lot(conn, sid, location=None)
    cands = _diff(conn, d)["to_pull"][0]["candidates"]
    assert [c["collection_id"] for c in cands] == [free, raided]
    assert [c["in_deck"] for c in cands] == [False, True]


def test_unlocated_lots_sorted_by_value_descending(conn, add_card):
    cheap, dear = add_card("Cheap"), add_card("Dear")
    conn.execute("UPDATE cards SET current_price_usd=0.5 WHERE scryfall_id=?", (cheap,))
    conn.execute("UPDATE cards SET current_price_usd=9 WHERE scryfall_id=?", (dear,))
    _lot(conn, cheap, location=None, quantity=2)
    _lot(conn, dear, location="  ")
    _lot(conn, dear, location="Box")  # located: excluded
    rows = q.unlocated_lots(conn)
    assert [r["card"] for r in rows] == ["Dear", "Cheap"]
    assert rows[1]["value"] == 1.0


def test_move_lot_splits_and_prorates_price(conn, add_card):
    sid = add_card("Sol Ring")
    lot = _lot(conn, sid, location="Box", quantity=3, price_paid=3.0)
    new = writes.move_lot(conn, lot, "Raktres", quantity=1)
    assert new != lot
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT collection_id, quantity, location, price_paid FROM collection")}
    assert rows[lot] == (2, "Box", 2.0)
    assert rows[new] == (1, "Raktres", 1.0)


def test_move_lot_whole_lot_keeps_id(conn, add_card):
    sid = add_card("Sol Ring")
    lot = _lot(conn, sid, location="Box", quantity=2)
    assert writes.move_lot(conn, lot, "Raktres", quantity=2) == lot
    assert writes.move_lot(conn, lot, "Box") == lot
    assert conn.execute("SELECT COUNT(*), location FROM collection").fetchone()[:] == (1, "Box")
