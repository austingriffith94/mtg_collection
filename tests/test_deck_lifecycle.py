"""Deck lifecycle — Phase 6 of the Workbench rework (see
IMPLEMENTATION_PLAN.md): brewing a deck whose cards are still elsewhere
(the sourcing plan and its donor-impact warnings), taking a deck apart to
feed another one, and the build_state flag that separates a brew from a
built deck whose locations were never recorded.
"""
import pytest

from dashboard_lib import queries as q
from dashboard_lib import writes


def _deck(conn, name, build_state=None):
    cur = conn.execute(
        "INSERT INTO decks (name, build_state) VALUES (?,?)", (name, build_state)
    )
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
    conn.execute(
        "INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)",
        (deck_id, sid, quantity),
    )
    conn.commit()


def _basic(conn, add_card, name="Swamp"):
    sid = add_card(name)
    conn.execute("UPDATE cards SET is_basic_land=1 WHERE scryfall_id=?", (sid,))
    conn.commit()
    return sid


def _location(conn, collection_id):
    return conn.execute(
        "SELECT location FROM collection WHERE collection_id=?", (collection_id,)
    ).fetchone()[0]


# ------------------------------------------------------------------
# build_state
# ------------------------------------------------------------------
def test_build_state_defaults_to_null_and_is_invisible_to_other_queries(conn, add_card):
    """The whole point of a nullable column: twelve existing decks need no
    backfill and nothing that already worked changes."""
    d = _deck(conn, "Vilis")
    sid = add_card("Sol Ring")
    _main(conn, d, sid)
    _lot(conn, sid, location="Vilis")

    assert q.deck_meta(conn, d)["build_state"] is None
    assert q.list_decks(conn)["build_state"].isna().all()
    assert q.deck_comparison(conn)["build_state"].isna().all()
    # Phase 4's diff and Phase 5's history are untouched by the new column.
    assert q.deck_reconciliation(conn, d)[0]["build"]["pct"] == 100.0
    assert q.deck_change_history(conn, d) == []


def test_set_build_state_round_trips_and_rejects_nonsense(conn):
    d = _deck(conn, "Grizzly")
    assert writes.set_build_state(conn, d, "brewing") == "Grizzly"
    assert q.deck_meta(conn, d)["build_state"] == "brewing"
    # "Mark as built" is a clear back to NULL, not another state.
    writes.set_build_state(conn, d, None)
    assert q.deck_meta(conn, d)["build_state"] is None
    with pytest.raises(ValueError, match="Unknown build state"):
        writes.set_build_state(conn, d, "retired")
    with pytest.raises(ValueError, match="Deck not found"):
        writes.set_build_state(conn, 999, "brewing")


# ------------------------------------------------------------------
# Sourcing plan (6a)
# ------------------------------------------------------------------
def test_sourcing_buckets_are_exclusive_and_sum_to_the_nonbasic_count(conn, add_card):
    """The regression this function exists to prevent: a card with one lot
    in the box AND one sleeved in another deck is ONE requirement, sourced
    from the box, and must not be counted in both buckets."""
    brew = _deck(conn, "Grizzly", build_state="brewing")
    other = _deck(conn, "Tuvasa")

    both = add_card("Sol Ring")          # a lot in the box and a lot in Tuvasa
    _main(conn, brew, both)
    _lot(conn, both, location="Box")
    _lot(conn, both, location="Tuvasa")
    _main(conn, other, both)

    boxed = add_card("Arcane Signet")
    _main(conn, brew, boxed)
    _lot(conn, boxed, location="Box")

    nowhere = add_card("Cultivate")      # owned, location unknown
    _main(conn, brew, nowhere)
    _lot(conn, nowhere, location=None)

    unowned = add_card("Mana Crypt")
    _main(conn, brew, unowned)

    here = add_card("Command Tower")
    _main(conn, brew, here)
    _lot(conn, here, location="Grizzly")

    swamp = _basic(conn, add_card)       # basics are not sourced at all
    _main(conn, brew, swamp, quantity=10)

    plan = q.deck_sourcing_plan(conn, brew)
    assert plan["needed"] == 5
    assert sum(plan["counts"].values()) == plan["needed"]
    assert plan["counts"] == {
        "sleeved": 1, "box": 2, "unlocated": 1, "other_deck": 0, "not_owned": 1
    }
    # Sol Ring is in the box bucket and nowhere else.
    where = {
        c["card"]: b
        for b, rows in plan["buckets"].items()
        for c in rows
    }
    assert where["Sol Ring"] == "box"
    assert plan["donors"] == []
    assert plan["located"] == 1 and plan["pct"] == pytest.approx(20.0)


def test_only_copy_in_another_deck_is_sourced_from_that_deck(conn, add_card):
    brew = _deck(conn, "Grizzly", build_state="brewing")
    tuvasa = _deck(conn, "Tuvasa")
    sid = add_card("Smothering Tithe")
    _main(conn, brew, sid)
    _main(conn, tuvasa, sid)
    _lot(conn, sid, location="Tuvasa")

    plan = q.deck_sourcing_plan(conn, brew)
    assert plan["counts"]["other_deck"] == 1
    assert plan["counts"]["not_owned"] == 0
    assert [(d["deck_name"], d["copies"]) for d in plan["donors"]] == [("Tuvasa", 1)]


def test_donor_impact_reports_the_donor_one_short(conn, add_card):
    """Taking a card whose only copy is in deck X leaves X one short — the
    warning that doesn't exist anywhere else in the app."""
    brew = _deck(conn, "Grizzly", build_state="brewing")
    tuvasa = _deck(conn, "Tuvasa")
    for name in ("Smothering Tithe", "Mystic Remora"):
        sid = add_card(name)
        _main(conn, brew, sid)
        _main(conn, tuvasa, sid)
        _lot(conn, sid, location="Tuvasa")

    donors = q.deck_sourcing_plan(conn, brew)["donors"]
    assert [(d["deck_name"], d["copies"], d["shortfall"]) for d in donors] == [("Tuvasa", 2, 2)]
    assert sorted(c["card"] for c in donors[0]["cards"]) == ["Mystic Remora", "Smothering Tithe"]


def test_raiding_a_leftover_lot_costs_the_donor_nothing(conn, add_card):
    """A lot sitting in a deck that no longer RUNS the card is free to take:
    copies, yes; shortfall, no."""
    brew = _deck(conn, "Grizzly", build_state="brewing")
    _deck(conn, "Tuvasa")
    sid = add_card("Smothering Tithe")
    _main(conn, brew, sid)
    _lot(conn, sid, location="Tuvasa")   # sleeved there, not on its list

    donors = q.deck_sourcing_plan(conn, brew)["donors"]
    assert [(d["deck_name"], d["copies"], d["shortfall"]) for d in donors] == [("Tuvasa", 1, 0)]


def test_donor_already_short_of_the_card_loses_nothing_further(conn, add_card):
    """Tuvasa runs two copies and holds one. Taking that one breaks exactly
    one of its satisfied needs, not two."""
    brew = _deck(conn, "Grizzly", build_state="brewing")
    tuvasa = _deck(conn, "Tuvasa")
    sid = add_card("Sol Ring")
    _main(conn, brew, sid)
    _main(conn, tuvasa, sid, quantity=2)
    _lot(conn, sid, location="Tuvasa")

    donors = q.deck_sourcing_plan(conn, brew)["donors"]
    assert donors[0]["shortfall"] == 1


def test_a_card_needing_two_copies_splits_across_buckets(conn, add_card):
    """One copy in the box, one nowhere: two buckets, two copies, still
    summing to the requirement."""
    brew = _deck(conn, "Grizzly", build_state="brewing")
    sid = add_card("Sol Ring")
    _main(conn, brew, sid, quantity=3)
    _lot(conn, sid, location="Box")

    plan = q.deck_sourcing_plan(conn, brew)
    assert plan["needed"] == 3
    assert plan["counts"]["box"] == 1 and plan["counts"]["not_owned"] == 2
    assert sum(plan["counts"].values()) == 3


def test_sourcing_plan_of_a_built_deck_is_all_sleeved(conn, add_card):
    d = _deck(conn, "Vilis")
    for name in ("Sol Ring", "Arcane Signet"):
        sid = add_card(name)
        _main(conn, d, sid)
        _lot(conn, sid, location="Vilis")
    plan = q.deck_sourcing_plan(conn, d)
    assert plan["counts"]["sleeved"] == 2 and plan["pct"] == 100.0
    assert plan["buckets"]["not_owned"] == []


def test_sourcing_plan_build_pct_matches_reconciliation(conn, add_card):
    """Two functions, one number — they share the cap-at-needed rule, so a
    surplus lot can't make a deck look more built than it is."""
    d = _deck(conn, "Vilis")
    a, b = add_card("Sol Ring"), add_card("Arcane Signet")
    _main(conn, d, a)
    _main(conn, d, b)
    _lot(conn, a, location="Vilis", quantity=3)   # surplus
    plan = q.deck_sourcing_plan(conn, d)
    assert plan["pct"] == q.deck_reconciliation(conn, d)[0]["build"]["pct"] == 50.0


def test_sourcing_plan_unknown_deck_is_none(conn):
    assert q.deck_sourcing_plan(conn, 999) is None


# ------------------------------------------------------------------
# Dismantle plan (6b)
# ------------------------------------------------------------------
def test_dismantle_splits_into_transfer_and_box(conn, add_card):
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")

    shared = add_card("Feed the Swarm")
    _main(conn, vilis, shared)
    _main(conn, gisa, shared)
    _lot(conn, shared, location="Vilis")

    only_vilis = add_card("Bolas's Citadel")
    _main(conn, vilis, only_vilis)
    _lot(conn, only_vilis, location="Vilis")

    plan = q.deck_dismantle_plan(conn, vilis, gisa)
    assert [c["card"] for c in plan["direct_transfer"]] == ["Feed the Swarm"]
    assert [c["card"] for c in plan["to_box"]] == ["Bolas's Citadel"]
    assert plan["sleeved_copies"] == 2
    assert plan["target_deck_name"] == "Gisa"


def test_dismantle_with_no_target_sends_everything_to_the_box(conn, add_card):
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    shared = add_card("Feed the Swarm")
    _main(conn, vilis, shared)
    _main(conn, gisa, shared)
    _lot(conn, shared, location="Vilis")

    plan = q.deck_dismantle_plan(conn, vilis)
    assert plan["direct_transfer"] == []
    assert [c["card"] for c in plan["to_box"]] == ["Feed the Swarm"]


def test_target_already_holding_the_card_absorbs_nothing(conn, add_card):
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    sid = add_card("Feed the Swarm")
    _main(conn, vilis, sid)
    _main(conn, gisa, sid)
    _lot(conn, sid, location="Vilis")
    _lot(conn, sid, location="Gisa")     # Gisa's copy is already sleeved

    plan = q.deck_dismantle_plan(conn, vilis, gisa)
    assert plan["direct_transfer"] == []
    assert [c["card"] for c in plan["to_box"]] == ["Feed the Swarm"]


def test_lots_sleeved_here_but_not_on_the_list_are_strays(conn, add_card):
    """Leftovers of past swaps: real cards in the sleeves with no list entry
    behind them, so they are not deck_changes material."""
    vilis = _deck(conn, "Vilis")
    on_list = add_card("Sol Ring")
    _main(conn, vilis, on_list)
    _lot(conn, on_list, location="Vilis")
    stray = add_card("Heirloom Blade")
    stray_lot = _lot(conn, stray, location="Vilis")

    plan = q.deck_dismantle_plan(conn, vilis)
    assert [c["card"] for c in plan["to_box"]] == ["Sol Ring"]
    assert [c["card"] for c in plan["strays"]] == ["Heirloom Blade"]

    assert writes.move_dismantle_strays(conn, vilis) == 1
    assert _location(conn, stray_lot) == "Box"


def test_dismantle_reports_what_the_target_still_needs(conn, add_card):
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    shared = add_card("Feed the Swarm")
    _main(conn, vilis, shared)
    _main(conn, gisa, shared)
    _lot(conn, shared, location="Vilis")
    missing = add_card("Gravecrawler")
    _main(conn, gisa, missing)

    plan = q.deck_dismantle_plan(conn, vilis, gisa)
    assert [(n["card"], n["short"]) for n in plan["target_needs"]] == [("Gravecrawler", 1)]


def test_dismantle_carries_each_decks_own_printing(conn, add_card):
    """Three printings that can differ: the sleeved lot's, the source list's
    and the target list's. Using the wrong one is how a dismantle silently
    fails to cut anything."""
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    src = add_card("Feed the Swarm", set_code="m21", collector_number="1")
    tgt = add_card("Feed the Swarm", set_code="j25", collector_number="2")
    lot_print = add_card("Feed the Swarm", set_code="plst", collector_number="3")
    _main(conn, vilis, src)
    _main(conn, gisa, tgt)
    _lot(conn, lot_print, location="Vilis")

    card = q.deck_dismantle_plan(conn, vilis, gisa)["direct_transfer"][0]
    assert card["scryfall_id"] == lot_print
    assert card["source_scryfall_id"] == src
    assert card["target_scryfall_id"] == tgt


def test_dismantle_plan_unknown_deck_is_none(conn):
    assert q.deck_dismantle_plan(conn, 999) is None


# ------------------------------------------------------------------
# Staging a dismantle, and applying it
# ------------------------------------------------------------------
def test_stage_dismantle_plans_both_sides_and_applies_nothing(conn, add_card):
    """Removals on the source, adds on the target, all 'planned' — nothing
    touched until confirmed."""
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    shared = add_card("Feed the Swarm")
    _main(conn, vilis, shared)
    _main(conn, gisa, shared)
    lot = _lot(conn, shared, location="Vilis")
    boxed = add_card("Bolas's Citadel")
    _main(conn, vilis, boxed)
    _lot(conn, boxed, location="Vilis")

    staged = writes.stage_dismantle(conn, vilis, gisa)
    assert len(staged["removed"]) == 2 and len(staged["added"]) == 1
    assert staged["skipped"] == []

    rows = writes.list_changes(conn, statuses=("planned",))
    assert {r["status"] for r in rows} == {"planned"}
    source_rows = [r for r in rows if r["deck_id"] == vilis]
    target_rows = [r for r in rows if r["deck_id"] == gisa]
    assert all(r["remove_scryfall_id"] and not r["add_scryfall_id"] for r in source_rows)
    assert all(r["add_scryfall_id"] and not r["remove_scryfall_id"] for r in target_rows)
    # The transfer names its destination; the box row names storage.
    assert {r["remove_name"]: r["dest_location"] for r in source_rows} == {
        "Feed the Swarm": "Gisa", "Bolas's Citadel": "Box",
    }
    # Nothing physical moved, and both mainboards are untouched.
    assert _location(conn, lot) == "Vilis"
    assert conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id=?", (vilis,)
    ).fetchone()[0] == 2

    # The removal is staged BEFORE the add, so change_id order applies the
    # cut first — otherwise the add would source a copy still in the sleeves.
    assert min(staged["removed"]) < min(staged["added"])


def test_direct_transfer_moves_the_lot_without_a_stop_at_box(conn, add_card):
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    sid = add_card("Feed the Swarm")
    _main(conn, vilis, sid)
    _main(conn, gisa, sid)
    lot = _lot(conn, sid, location="Vilis")

    staged = writes.stage_dismantle(conn, vilis, gisa)
    results = writes.apply_changes(conn, staged["removed"] + staged["added"])
    assert all(r["ok"] for r in results), [r["error"] for r in results]

    # One lot, now in Gisa — not a new lot, and never "Box".
    assert _location(conn, lot) == "Gisa"
    assert conn.execute("SELECT COUNT(*) FROM collection").fetchone()[0] == 1
    # Source list lost the card, target list kept it.
    assert conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id=?", (vilis,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id=?", (gisa,)
    ).fetchone()[0] == 1


def test_applying_a_box_removal_returns_the_lot_to_storage(conn, add_card):
    vilis = _deck(conn, "Vilis")
    sid = add_card("Bolas's Citadel")
    _main(conn, vilis, sid)
    lot = _lot(conn, sid, location="Vilis")

    staged = writes.stage_dismantle(conn, vilis)
    results = writes.apply_changes(conn, staged["removed"])
    assert all(r["ok"] for r in results), [r["error"] for r in results]
    assert _location(conn, lot) == "Box"
    assert conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id=?", (vilis,)
    ).fetchone()[0] == 0


def test_applied_dismantle_lands_in_both_decks_history(conn, add_card):
    """The payoff of staging it as deck_changes rows: Phase 5's timeline
    picks it up on both decks with no new write path."""
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    sid = add_card("Feed the Swarm")
    _main(conn, vilis, sid)
    _main(conn, gisa, sid)
    _lot(conn, sid, location="Vilis")

    staged = writes.stage_dismantle(conn, vilis, gisa)
    writes.apply_changes(conn, staged["removed"] + staged["added"])

    vilis_changes = [e for e in q.deck_change_history(conn, vilis) if e["kind"] == "change"]
    gisa_changes = [e for e in q.deck_change_history(conn, gisa) if e["kind"] == "change"]
    assert [c["remove_card"] for c in vilis_changes] == ["Feed the Swarm"]
    assert [c["add_card"] for c in gisa_changes] == ["Feed the Swarm"]


def test_dismantle_selection_is_respected(conn, add_card):
    """An explicit empty selection stages nothing — "leave this card in the
    deck for now"."""
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    shared = add_card("Feed the Swarm")
    _main(conn, vilis, shared)
    _main(conn, gisa, shared)
    _lot(conn, shared, location="Vilis")
    boxed = add_card("Bolas's Citadel")
    _main(conn, vilis, boxed)
    _lot(conn, boxed, location="Vilis")

    staged = writes.stage_dismantle(conn, vilis, gisa, box_oracle_ids=[])
    assert len(staged["removed"]) == 1 and len(staged["added"]) == 1
    assert [r["remove_name"] for r in writes.list_changes(conn, deck_id=vilis)] == [
        "Feed the Swarm"
    ]


def test_dismantle_skips_a_card_already_on_the_targets_shortlist(conn, add_card):
    """The duplicate guard add_change() applies everywhere else: a dismantle
    must not create a second claim on one copy."""
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    sid = add_card("Feed the Swarm")
    _main(conn, vilis, sid)
    _main(conn, gisa, sid)
    _lot(conn, sid, location="Vilis")
    writes.add_change(conn, gisa, add_scryfall_id=sid, add_name="Feed the Swarm")

    staged = writes.stage_dismantle(conn, vilis, gisa)
    assert staged["added"] == [] and staged["removed"] == []
    assert staged["skipped"] == [("Feed the Swarm", "already on Gisa's shortlist")]


def test_staging_the_same_deck_twice_queues_one_cut_not_two(conn, add_card):
    """Feeding two different decks from one dismantle: the second pass finds
    the cut already queued and skips the card rather than promising the same
    copy twice. The guard fires before the allocator ever sees it."""
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    raktres = _deck(conn, "Raktres")
    sid = add_card("Feed the Swarm")
    for d in (vilis, gisa, raktres):
        _main(conn, d, sid)
    _lot(conn, sid, location="Vilis")

    first = writes.stage_dismantle(conn, vilis, gisa)
    assert len(first["removed"]) == 1 and len(first["added"]) == 1

    second = writes.stage_dismantle(conn, vilis, raktres)
    assert second["removed"] == [] and second["added"] == []
    assert second["skipped"] == [("Feed the Swarm", "already queued to leave Vilis")]


def test_a_dismantle_claiming_a_copy_someone_else_wants_is_a_conflict(conn, add_card):
    """The dismantle's add and an unrelated shortlist row both want the one
    copy still sleeved in Vilis. The allocator promises it to neither, and
    plan_conflicts names the card as the decision to make."""
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    raktres = _deck(conn, "Raktres")
    sid = add_card("Feed the Swarm")
    cut = add_card("Heirloom Blade")
    for d in (vilis, gisa):
        _main(conn, d, sid)
    _main(conn, raktres, cut)
    _lot(conn, sid, location="Vilis")

    writes.stage_dismantle(conn, vilis, gisa)
    writes.add_change(conn, raktres, add_scryfall_id=sid, add_name="Feed the Swarm",
                      remove_scryfall_id=cut, remove_name="Heirloom Blade",
                      status="planned")

    changes = q.open_changes_dataframe(conn, statuses=("planned",))
    verdicts = q.plan_availability(conn, changes)
    adds = [v for v in verdicts.values() if v["verdict"] != "no_add"]
    assert len(adds) == 2
    assert {v["verdict"] for v in adds} == {"in_other_deck"}
    assert all(v["claimed"] == 0 for v in adds)

    conflicts = q.plan_conflicts(conn, changes, availability=verdicts)
    assert [(c["card"], c["shortfall"]) for c in conflicts] == [("Feed the Swarm", 2)]


def test_once_the_copy_reaches_storage_the_second_claim_is_told_who_took_it(conn, add_card):
    """The same copy, now unsleeved by the dismantle's removal: the first
    add claims it and the second is told which change got there first."""
    vilis = _deck(conn, "Vilis")
    gisa = _deck(conn, "Gisa")
    raktres = _deck(conn, "Raktres")
    sid = add_card("Feed the Swarm")
    for d in (vilis, gisa, raktres):
        _main(conn, d, sid)
    lot = _lot(conn, sid, location="Vilis")

    staged = writes.stage_dismantle(conn, vilis)
    writes.apply_changes(conn, staged["removed"])
    assert _location(conn, lot) == "Box"

    first = writes.add_change(conn, gisa, add_scryfall_id=sid, add_name="Feed the Swarm",
                              remove_scryfall_id=sid, remove_name="Feed the Swarm",
                              status="planned")
    second = writes.add_change(conn, raktres, add_scryfall_id=sid, add_name="Feed the Swarm",
                               remove_scryfall_id=sid, remove_name="Feed the Swarm",
                               status="planned")
    changes = q.open_changes_dataframe(conn, statuses=("planned",))
    verdicts = q.plan_availability(conn, changes)
    assert verdicts[first]["verdict"] == "available"
    assert verdicts[second]["verdict"] == "claimed_by"
    assert [c["change_id"] for c in verdicts[second]["claimed_by"]] == [first]


def test_stage_dismantle_unknown_deck_raises(conn):
    with pytest.raises(ValueError, match="Deck not found"):
        writes.stage_dismantle(conn, 999)


# ------------------------------------------------------------------
# The apply paths the lifecycle rows need, guarded against regressions in
# the swap path they share.
# ------------------------------------------------------------------
def test_pure_addition_sleeves_a_card_without_cutting_one(conn, add_card):
    gisa = _deck(conn, "Gisa")
    sid = add_card("Gravecrawler")
    lot = _lot(conn, sid, location="Box")
    cur = conn.execute(
        """INSERT INTO deck_changes (deck_id, status, add_scryfall_id, add_name, planned_at)
           VALUES (?, 'planned', ?, ?, CURRENT_TIMESTAMP)""",
        (gisa, sid, "Gravecrawler"),
    )
    conn.commit()
    writes.apply_change(conn, cur.lastrowid)

    assert _location(conn, lot) == "Gisa"
    assert conn.execute(
        "SELECT quantity FROM deck_cards WHERE deck_id=? AND scryfall_id=?", (gisa, sid)
    ).fetchone()[0] == 1


def test_an_unresolved_add_side_is_still_an_error_not_a_pure_removal(conn, add_card):
    """A planned swap whose add side never resolved to a printing must not
    quietly become "cut a card and add nothing"."""
    vilis = _deck(conn, "Vilis")
    sid = add_card("Sol Ring")
    _main(conn, vilis, sid)
    cid = writes.add_change(
        conn, vilis, add_name="some card typed by hand",
        remove_scryfall_id=sid, remove_name="Sol Ring", status="planned",
    )
    with pytest.raises(ValueError, match="isn't linked to a printing"):
        writes.apply_change(conn, cid)
    # Nothing was cut.
    assert conn.execute(
        "SELECT COUNT(*) FROM deck_cards WHERE deck_id=?", (vilis,)
    ).fetchone()[0] == 1


def test_removal_of_a_card_no_longer_on_the_list_still_raises(conn, add_card):
    vilis = _deck(conn, "Vilis")
    sid = add_card("Sol Ring")
    _main(conn, vilis, sid)
    _lot(conn, sid, location="Vilis")
    staged = writes.stage_dismantle(conn, vilis)
    conn.execute("DELETE FROM deck_cards WHERE deck_id=?", (vilis,))
    conn.commit()
    with pytest.raises(ValueError, match="no longer in this deck's mainboard"):
        writes.apply_change(conn, staged["removed"][0])


# ------------------------------------------------------------------
# Presentation: the tile strip and the printable pull sheet
# ------------------------------------------------------------------
def test_tile_strip_leads_with_the_lifecycle_flag(conn, add_card):
    from dashboard_lib import card_view as cv

    assert cv.deck_status_strip(
        {"build_state": "brewing", "planned": 0, "ideas": 3, "games": 0, "build_pct": 4.0}
    ) == "🔨 brewing · 3 ideas · 4% built"
    assert cv.deck_status_strip(
        {"build_state": "dismantled", "planned": 0, "ideas": 0, "games": 0, "build_pct": None}
    ) == "🔧 dismantled"
    # A normal built deck reads exactly as it did before Phase 6.
    assert cv.deck_status_strip(
        {"build_state": None, "planned": 9, "ideas": 18, "games": 3, "wins": 1, "losses": 2,
         "build_pct": 100.0}
    ) == "9 planned · 18 ideas · 1-2"


def test_pull_sheet_groups_by_where_the_card_is(conn, add_card):
    from dashboard_lib import deck_printout

    brew = _deck(conn, "Grizzly", build_state="brewing")
    tuvasa = _deck(conn, "Tuvasa")
    boxed = add_card("Sol Ring")
    _main(conn, brew, boxed)
    _lot(conn, boxed, location="Lands Box")
    contested = add_card("Smothering Tithe")
    _main(conn, brew, contested)
    _main(conn, tuvasa, contested)
    _lot(conn, contested, location="Tuvasa")
    unowned = add_card("Mana Crypt")
    _main(conn, brew, unowned)

    html = deck_printout.build_pull_sheet_html(q.deck_sourcing_plan(conn, brew))
    assert "Pull sheet — Grizzly" in html
    assert "From the box" in html and "Lands Box" in html
    assert "From another deck" in html and "leaves it 1 short" in html
    assert "Not owned" in html and "Mana Crypt" in html
    # 3 non-basics, 0% sleeved, and 2 to pull — the unowned one isn't
    # something you can fetch, so it doesn't inflate the pull count.
    assert "3 non-basics" in html and "0% sleeved" in html and "2 to pull" in html
    # A bucket with no cards prints no heading at all.
    assert "Already sleeved here" not in html


def test_pull_sheet_of_a_finished_deck_says_so(conn, add_card):
    from dashboard_lib import deck_printout

    d = _deck(conn, "Vilis")
    sid = add_card("Sol Ring")
    _main(conn, d, sid)
    _lot(conn, sid, location="Vilis")
    html = deck_printout.build_pull_sheet_html(q.deck_sourcing_plan(conn, d))
    assert "0 to pull" in html and "100% sleeved" in html
    assert "Already sleeved here" in html


def test_pull_sheet_escapes_card_names(conn, add_card):
    """Card names carry apostrophes and commas; a name must never reach the
    document as markup."""
    from dashboard_lib import deck_printout

    d = _deck(conn, "Grizzly", build_state="brewing")
    sid = add_card("<script>Bolas's Citadel</script>")
    _main(conn, d, sid)
    html = deck_printout.build_pull_sheet_html(q.deck_sourcing_plan(conn, d))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
