"""Reconciliation — Phase 4 of the Workbench rework (see
docs/archive/IMPLEMENTATION_PLAN.md): unlocated lots, the per-deck list-vs-physical diff
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


# --- Deck flags (Workbench): untagged cards + flattened location gaps ---

def test_untagged_lists_only_cards_with_no_tag_in_that_deck(conn, add_card):
    a, b = _deck(conn, "Raktres"), _deck(conn, "Gisa")
    sol, crypt = add_card("Sol Ring"), add_card("Blood Crypt")
    swamp = _basic(conn, add_card)
    for d in (a, b):
        _main(conn, d, sol)
    _main(conn, a, crypt)
    _main(conn, a, swamp)
    writes.set_card_tags(conn, a, sol, "rule0", ["fast mana"])  # any tag type counts
    got = [(r["deck_name"], r["card"]) for r in q.untagged_deck_cards(conn)]
    # Tags are deck-scoped: Sol Ring tagged in Raktres is still untagged in Gisa.
    assert got == [("Gisa", "Sol Ring"), ("Raktres", "Blood Crypt")]


def test_location_mismatches_say_where_the_copies_are(conn, add_card):
    a, b = _deck(conn, "Raktres"), _deck(conn, "Gisa")
    sol, crypt, blade, bolt = (add_card(n) for n in
                               ("Sol Ring", "Blood Crypt", "Heirloom Blade", "Lightning Bolt"))
    _main(conn, a, sol)
    _lot(conn, sol, location="Gisa")
    _main(conn, a, crypt)
    _lot(conn, crypt, location="Box", quantity=2)
    _main(conn, a, bolt)                       # never owned
    _lot(conn, blade, location="Raktres")      # sleeved, not on the list
    got = {(r["card"], r["kind"]): (r["qty"], r["where"]) for r in q.location_mismatches(conn)
           if r["deck_id"] == a}
    assert got == {
        ("Blood Crypt", "missing"): (1, "Box ×2"),
        ("Heirloom Blade", "extra"): (1, "sleeved in Raktres, list runs 0"),
        ("Lightning Bolt", "missing"): (1, "not owned"),
        ("Sol Ring", "missing"): (1, "deck: Gisa ×1"),
    }
    # Gisa's stray Sol Ring is its own "extra" row.
    assert [(r["card"], r["kind"]) for r in q.location_mismatches(conn) if r["deck_id"] == b] == [
        ("Sol Ring", "extra")]


def test_deck_tag_labels_are_per_deck_and_per_type(conn, add_card):
    a, b = _deck(conn, "Raktres"), _deck(conn, "Gisa")
    sol, crypt = add_card("Sol Ring"), add_card("Blood Crypt")
    writes.set_card_tags(conn, a, sol, "strategy", ["Ramp", "artifact"])
    writes.set_card_tags(conn, a, crypt, "strategy", ["Ramp"])
    writes.set_card_tags(conn, a, crypt, "rule0", ["fast mana"])
    writes.set_card_tags(conn, b, sol, "strategy", ["Tokens"])
    assert q.deck_tag_labels(conn, "strategy") == {a: ["artifact", "Ramp"], b: ["Tokens"]}
    assert q.deck_tag_labels(conn)[a] == ["artifact", "fast mana", "Ramp"]


def _mismatch(conn, deck_id, card):
    return next(m for m in q.location_mismatches(conn) if m["deck_id"] == deck_id and m["card"] == card)


def test_fixing_a_missing_row_pulls_storage_before_another_deck(conn, add_card):
    a, _b = _deck(conn, "Raktres"), _deck(conn, "Gisa")
    sol = add_card("Sol Ring")
    _main(conn, a, sol)
    _lot(conn, sol, location="Gisa")
    _lot(conn, sol, location="Box")
    writes.fix_location_mismatch(conn, _mismatch(conn, a, "Sol Ring"))  # short 1: the Box copy
    got = sorted(tuple(r) for r in conn.execute("SELECT location, quantity FROM collection"))
    assert got == [("Gisa", 1), ("Raktres", 1)]
    assert not [m for m in q.location_mismatches(conn) if m["deck_id"] == a]


def test_fixing_a_missing_row_takes_from_several_lots_until_covered(conn, add_card):
    a = _deck(conn, "Raktres")
    sol = add_card("Sol Ring")
    _main(conn, a, sol, quantity=3)
    _lot(conn, sol, location="Box", quantity=2)
    _lot(conn, sol, location="Binder", quantity=2)
    writes.fix_location_mismatch(conn, _mismatch(conn, a, "Sol Ring"))
    here = conn.execute("SELECT SUM(quantity) FROM collection WHERE location='Raktres'").fetchone()[0]
    total = conn.execute("SELECT SUM(quantity) FROM collection").fetchone()[0]
    assert (here, total) == (3, 4)


def test_fixing_an_extra_row_puts_only_the_surplus_away(conn, add_card):
    a = _deck(conn, "Raktres")
    blade = add_card("Heirloom Blade")
    _main(conn, a, blade)
    _lot(conn, blade, location="Raktres", quantity=3)
    writes.fix_location_mismatch(conn, _mismatch(conn, a, "Heirloom Blade"), dest="Binder")
    got = sorted(tuple(r) for r in conn.execute("SELECT location, quantity FROM collection"))
    assert got == [("Binder", 2), ("Raktres", 1)]


def test_fixing_an_unowned_card_says_buy_it(conn, add_card):
    a = _deck(conn, "Raktres")
    _main(conn, a, add_card("Lightning Bolt"))
    try:
        writes.fix_location_mismatch(conn, _mismatch(conn, a, "Lightning Bolt"))
    except ValueError as exc:
        assert "Buy list" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_two_rows_sharing_one_lot_move_it_once(conn, add_card):
    # Both decks are missing Sol Ring and both rows list the one Box copy.
    # Fixed in one batch, the second must not pull it back out of the first.
    a, b = _deck(conn, "Raktres"), _deck(conn, "Gisa")
    sol = add_card("Sol Ring")
    _main(conn, a, sol)
    _main(conn, b, sol)
    _lot(conn, sol, location="Box")
    rows = [m for m in q.location_mismatches(conn) if m["card"] == "Sol Ring"]
    writes.fix_location_mismatch(conn, rows[0])
    try:
        writes.fix_location_mismatch(conn, rows[1])
    except ValueError as exc:
        assert "moved since" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert [tuple(r) for r in conn.execute("SELECT location FROM collection")] == [(rows[0]["deck_name"],)]
