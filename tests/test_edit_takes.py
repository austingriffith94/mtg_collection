"""EDIT_FLOW_PLAN.md Phase 3: taking a card from another deck, as a linked
pair that shows on both decks.

The world: Gisa (the donor, running Sol Ring with its copy sleeved there),
Prosper (the deck taking it) and Tuvasa (a third deck, for "someone else
wants it too"). The rules covered, in order:

  - the reads: card_sources() lists decks whose list runs the card
  - a direct take (transfer_between_decks / add_deck_card with a deck
    source): both lists, the lot (Gisa → Prosper, no stop in the box), two
    linked history rows, and every refusal before anything is written
  - a brewing receiver: its list changes, Gisa's half waits as a planned
    removal ("lopsided"), and dropping the card from the brew drops it
  - a staged take (add_change with source='deck'): the pair, the guards,
    applying / dropping / demoting / re-pointing / reopening either half
  - stage_dismantle() links its pairs and says where they come from
  - the allocator and conflicts see the promise
  - delete / rename of a deck in a pair
  - the Workbench's one-row-per-pair view, the Board, Card Lookup, History
"""
import os
import sqlite3

import pytest

from dashboard_lib import deck_board, formatting as fmt, queries as q, writes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RING, BLADE, SIGNET = "Sol Ring", "Heirloom Blade", "Arcane Signet"


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
    for name in (RING, BLADE, SIGNET):
        conn.execute("INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id)"
                     " VALUES (?,?,'cmd','1',?)", (name, name, f"oracle-{name}"))
    for deck in ("Gisa", "Prosper", "Tuvasa"):
        conn.execute("INSERT INTO decks (name) VALUES (?)", (deck,))
    conn.commit()
    yield conn
    conn.close()


def _deck(c, name):
    return c.execute("SELECT deck_id FROM decks WHERE name=?", (name,)).fetchone()[0]


def _gisa_runs_ring(c, sleeved=True):
    """Gisa's list runs Sol Ring ×1 with (by default) its copy sleeved there."""
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (_deck(c, "Gisa"), RING))
    c.commit()
    if sleeved:
        return writes.add_collection_lot(c, RING, quantity=1, location="Gisa")
    return None


def _list(c, deck, card=RING):
    return q.deck_list_quantity(c, _deck(c, deck), card)


def _lots(c, card=RING):
    return sorted((r[0] or "", r[1]) for r in c.execute(
        "SELECT location, quantity FROM collection WHERE scryfall_id=?", (card,)))


def _rows(c, deck, status=None):
    sql = "SELECT * FROM deck_changes WHERE deck_id=?"
    params = [_deck(c, deck)]
    if status:
        sql += " AND status=?"
        params.append(status)
    return [dict(r) for r in c.execute(sql + " ORDER BY change_id", params)]


def _brew(c, deck="Prosper"):
    writes.set_build_state(c, _deck(c, deck), "brewing")


# ------------------------------------------------------------------
# The reads
# ------------------------------------------------------------------
def test_card_sources_lists_decks_that_run_the_card(c):
    _gisa_runs_ring(c)
    writes.add_collection_lot(c, RING, quantity=1, location="Tuvasa")   # a stray: sleeved, not listed
    out = q.card_sources(c, RING, _deck(c, "Prosper"))
    assert out["decks"] == [{"deck": "Gisa", "quantity": 1, "listed": 1},
                            {"deck": "Tuvasa", "quantity": 1, "listed": 0}]
    choices = fmt.source_choices(out, q.storage_locations(c))
    # Only a deck whose LIST runs it is a source; Tuvasa's stray isn't.
    assert ("Deck: Gisa — 1 sleeved", "deck", "Gisa") in choices
    assert not any(src == "deck" and loc == "Tuvasa" for _l, src, loc in choices)


def test_a_listed_but_unsleeved_deck_says_so_in_the_picker(c):
    _gisa_runs_ring(c, sleeved=False)
    out = q.card_sources(c, RING, _deck(c, "Prosper"))
    assert fmt.deck_choice_label(out["decks"][0]) == "Deck: Gisa — on its list, not recorded as sleeved"


# ------------------------------------------------------------------
# A direct take
# ------------------------------------------------------------------
def test_a_direct_take_moves_the_card_between_lists_and_the_copy_between_decks(c):
    lot = _gisa_runs_ring(c)
    out = writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 1)

    assert _list(c, "Gisa") == 0 and _list(c, "Prosper") == 1
    assert _lots(c) == [("Prosper", 1)]                      # no stop in the box
    assert out["moves"] == [{"card": RING, "quantity": 1, "collection_id": lot, "from": "Gisa",
                             "to": "Prosper", "kind": "move"}]
    (gone,) = _rows(c, "Gisa", "applied")
    (came,) = _rows(c, "Prosper", "applied")
    assert gone["remove_scryfall_id"] == RING and gone["dest_location"] == "Prosper"
    assert came["add_scryfall_id"] == RING and (came["source"], came["source_location"]) == ("deck", "Gisa")
    assert gone["linked_change_id"] == came["change_id"] and came["linked_change_id"] == gone["change_id"]
    assert not out["held"]
    # The move log ties each move to its history row.
    assert c.execute("SELECT reason, change_id FROM collection_moves ORDER BY move_id DESC LIMIT 1"
                     ).fetchone()[:] == ("deck_edit", gone["change_id"])


def test_add_deck_card_with_a_deck_source_is_a_take(c):
    _gisa_runs_ring(c)
    moves = writes.add_deck_card(c, _deck(c, "Prosper"), RING, 1, source="deck", source_location="Gisa")
    assert [(m["from"], m["to"]) for m in moves] == [("Gisa", "Prosper")]
    assert _list(c, "Gisa") == 0 and _list(c, "Prosper") == 1


def test_a_take_of_part_of_a_stack_leaves_the_rest_on_the_donor(c):
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,3)", (_deck(c, "Gisa"), RING))
    c.commit()
    writes.add_collection_lot(c, RING, quantity=3, location="Gisa")
    writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 2)
    assert _list(c, "Gisa") == 1 and _list(c, "Prosper") == 2
    assert _lots(c) == [("Gisa", 1), ("Prosper", 2)]


@pytest.mark.parametrize("setup, args, match", [
    ("none", ("Gisa", "Prosper", 1), "doesn't run Sol Ring"),
    ("listed", ("Gisa", "Prosper", 2), r"only runs Sol Ring ×1"),
    ("listed", ("Gisa", "Gisa", 1), "can't take a card from itself"),
    ("unsleeved", ("Gisa", "Prosper", 1), "no copy to hand over"),
    ("dismantled", ("Gisa", "Prosper", 1), "is dismantled"),
])
def test_a_direct_take_refuses_before_writing_anything(c, setup, args, match):
    if setup != "none":
        _gisa_runs_ring(c, sleeved=setup != "unsleeved")
    if setup == "dismantled":
        writes.set_build_state(c, _deck(c, "Prosper"), "dismantled")
    before = (_lots(c), [dict(r) for r in c.execute("SELECT * FROM deck_cards")],
              c.execute("SELECT COUNT(*) FROM deck_changes").fetchone()[0])
    frm, to, n = args
    with pytest.raises(ValueError, match=match):
        writes.transfer_between_decks(c, _deck(c, frm), _deck(c, to), RING, n)
    assert (_lots(c), [dict(r) for r in c.execute("SELECT * FROM deck_cards")],
            c.execute("SELECT COUNT(*) FROM deck_changes").fetchone()[0]) == before


def test_a_second_take_of_one_copy_is_refused(c):
    _gisa_runs_ring(c)
    writes.add_change(c, _deck(c, "Tuvasa"), add_scryfall_id=RING, source="deck", source_location="Gisa")
    with pytest.raises(ValueError, match="already queued to leave Gisa \\(promised to Tuvasa\\)"):
        writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 1)
    with pytest.raises(ValueError, match="already queued to leave Gisa"):
        writes.add_change(c, _deck(c, "Prosper"), add_scryfall_id=RING, source="deck", source_location="Gisa")


def test_a_direct_take_supersedes_this_decks_own_staged_take(c):
    _gisa_runs_ring(c)
    idea = writes.add_change(c, _deck(c, "Prosper"), add_scryfall_id=RING, source="deck", source_location="Gisa")
    donor_half = writes.get_change(c, idea)["linked_change_id"]
    writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 1)
    assert writes.get_change(c, idea)["status"] == "dropped"
    assert writes.get_change(c, donor_half)["status"] == "dropped"
    assert _lots(c) == [("Prosper", 1)]
    assert q.open_takes(c) == []


# ------------------------------------------------------------------
# A brewing receiver: the list now, the copy at Build it
# ------------------------------------------------------------------
def test_a_brewing_deck_takes_the_card_onto_its_list_and_leaves_the_copy(c):
    _gisa_runs_ring(c)
    _brew(c)
    out = writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 1)

    assert out["held"]
    assert _list(c, "Prosper") == 1 and _list(c, "Gisa") == 1   # Gisa's list untouched
    assert _lots(c) == [("Gisa", 1)]                            # and its sleeves
    assert [m["kind"] for m in out["moves"]] == ["brewing"]
    assert out["moves"][0]["source_location"] == "Gisa"
    assert card_label(out["moves"][0]) == "Brewing — Sol Ring will be pulled from Gisa when you build the deck."
    (came,) = _rows(c, "Prosper", "applied")
    (promise,) = _rows(c, "Gisa", "planned")
    assert promise["dest_location"] == "Prosper" and promise["linked_change_id"] == came["change_id"]
    assert deck_board.leaving_payload(q.open_takes(c, deck_id=_deck(c, "Gisa")))[0]["receiver_status"] == "applied"


def card_label(move):
    from dashboard_lib import card_view
    return card_view.move_label(move)


def test_dropping_the_card_from_the_brew_drops_gisas_promise(c):
    _gisa_runs_ring(c)
    _brew(c)
    writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 1)
    writes.remove_deck_card(c, _deck(c, "Prosper"), RING)
    (promise,) = _rows(c, "Gisa")
    assert promise["status"] == "dropped" and writes.TAKE_DROPPED_NOTE in promise["notes"]


def test_applying_the_lopsided_half_moves_gisas_copy_into_the_brew(c):
    # What Build it (Phase 5) will do; until then, Apply on the Workbench.
    lot = _gisa_runs_ring(c)
    _brew(c)
    out = writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 1)
    outcome = writes.apply_change(c, out["donor_change_id"])
    assert _list(c, "Gisa") == 0
    assert _lots(c) == [("Prosper", 1)]
    assert [(m["collection_id"], m["from"], m["to"]) for m in outcome["moves"]] == [(lot, "Gisa", "Prosper")]


# ------------------------------------------------------------------
# A staged take
# ------------------------------------------------------------------
def _staged(c, status="idea", **kw):
    return writes.add_change(c, _deck(c, "Prosper"), add_scryfall_id=RING, status=status,
                             source="deck", source_location="Gisa", **kw)


def test_a_staged_take_writes_a_linked_planned_removal_on_the_donor(c):
    _gisa_runs_ring(c)
    idea = _staged(c)
    row = writes.get_change(c, idea)
    donor = writes.get_change(c, row["linked_change_id"])
    assert (row["status"], row["source"], row["source_location"]) == ("idea", "deck", "Gisa")
    assert (donor["status"], donor["deck_id"], donor["remove_scryfall_id"], donor["dest_location"]) == \
        ("planned", _deck(c, "Gisa"), RING, "Prosper")
    assert donor["linked_change_id"] == idea
    assert donor["change_id"] < idea                       # removal first, as stage_dismantle writes
    assert _lots(c) == [("Gisa", 1)] and _list(c, "Gisa") == 1   # nothing moves yet


def test_a_staged_take_can_be_planned_with_no_card_to_cut(c):
    _gisa_runs_ring(c)
    planned = _staged(c, status="planned")
    assert writes.get_change(c, planned)["status"] == "planned"
    idea = writes.add_change(c, _deck(c, "Tuvasa"), add_scryfall_id=BLADE)
    with pytest.raises(ValueError, match="Pick a card for this to replace"):
        writes.promote_change(c, idea)        # an ordinary idea still needs one


def test_promoting_a_take_idea_needs_no_cut(c):
    _gisa_runs_ring(c)
    idea = _staged(c)
    writes.promote_change(c, idea)
    assert writes.get_change(c, idea)["status"] == "planned"


@pytest.mark.parametrize("side", ["receiver", "donor"])
def test_applying_either_half_applies_the_pair_removal_first(c, side):
    lot = _gisa_runs_ring(c)
    receiver = _staged(c, status="planned")
    donor = writes.get_change(c, receiver)["linked_change_id"]
    outcome = writes.apply_change(c, receiver if side == "receiver" else donor)

    assert {writes.get_change(c, i)["status"] for i in (receiver, donor)} == {"applied"}
    assert _list(c, "Gisa") == 0 and _list(c, "Prosper") == 1
    assert _lots(c) == [("Prosper", 1)]
    assert [(m["collection_id"], m["from"], m["to"]) for m in outcome["moves"]] == [(lot, "Gisa", "Prosper")]
    assert outcome["pair"] == {"card": RING, "from_deck": "Gisa", "to_deck": "Prosper", "held": False,
                               "partner_id": donor if side == "receiver" else receiver}


def test_a_pair_whose_receiver_is_an_idea_is_refused_from_either_side(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    donor = writes.get_change(c, receiver)["linked_change_id"]
    with pytest.raises(ValueError, match="Prosper's side of this take is still an idea"):
        writes.apply_change(c, donor)
    assert _list(c, "Gisa") == 1 and _lots(c) == [("Gisa", 1)]


def test_a_pair_is_checked_on_both_sides_before_either_is_written(c):
    _gisa_runs_ring(c, sleeved=False)       # Gisa's copy was never recorded as sleeved
    receiver = _staged(c, status="planned")
    with pytest.raises(ValueError, match="no copy to hand over"):
        writes.apply_change(c, receiver)
    assert _list(c, "Gisa") == 1 and _list(c, "Prosper") == 0
    assert {r["status"] for r in _rows(c, "Gisa") + _rows(c, "Prosper")} == {"planned"}


def test_apply_changes_reports_a_pair_once(c):
    _gisa_runs_ring(c)
    receiver = _staged(c, status="planned")
    donor = writes.get_change(c, receiver)["linked_change_id"]
    results = writes.apply_changes(c, [donor, receiver])
    assert len(results) == 1 and results[0]["ok"]
    assert fmt.apply_result_line(results[0]) == "✅ Took Sol Ring from Gisa into Prosper."


def test_a_planned_pair_into_a_brew_applies_only_the_brews_half(c):
    _gisa_runs_ring(c)
    receiver = _staged(c, status="planned")
    donor = writes.get_change(c, receiver)["linked_change_id"]
    _brew(c)
    (result,) = writes.apply_changes(c, [receiver, donor])
    assert result["ok"] and result["pair"]["held"]
    assert writes.get_change(c, receiver)["status"] == "applied"
    assert writes.get_change(c, donor)["status"] == "planned"    # Gisa keeps it until Build it
    assert _list(c, "Prosper") == 1 and _list(c, "Gisa") == 1 and _lots(c) == [("Gisa", 1)]
    assert "keeps its copy until Prosper is built" in fmt.apply_result_line(result)


@pytest.mark.parametrize("side", ["receiver", "donor"])
def test_dropping_either_half_drops_both_and_says_so(c, side):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    donor = writes.get_change(c, receiver)["linked_change_id"]
    lines = writes.drop_change(c, receiver if side == "receiver" else donor)
    assert {writes.get_change(c, i)["status"] for i in (receiver, donor)} == {"dropped"}
    assert lines == (["Also dropped Gisa's side of the take: Sol Ring stays in Gisa."] if side == "receiver"
                     else ["Also dropped Prosper's side of the take: Prosper no longer gets Sol Ring."])


def test_dropping_a_lopsided_promise_leaves_the_brews_list_alone(c):
    _gisa_runs_ring(c)
    _brew(c)
    out = writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 1)
    assert writes.drop_change(c, out["donor_change_id"]) == []
    assert writes.get_change(c, out["receiver_change_id"])["status"] == "applied"
    assert _list(c, "Prosper") == 1


def test_demoting_a_take_keeps_the_promise_but_the_donor_half_cannot_demote(c):
    _gisa_runs_ring(c)
    receiver = _staged(c, status="planned")
    donor = writes.get_change(c, receiver)["linked_change_id"]
    with pytest.raises(ValueError, match="promised to Prosper"):
        writes.demote_change(c, donor)
    writes.demote_change(c, receiver)
    assert writes.get_change(c, receiver)["status"] == "idea"
    assert writes.get_change(c, donor)["status"] == "planned"


def test_repointing_a_take_relinks_the_pair(c):
    _gisa_runs_ring(c)
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (_deck(c, "Tuvasa"), RING))
    c.commit()
    receiver = _staged(c)
    first = writes.get_change(c, receiver)["linked_change_id"]

    writes.update_change(c, receiver, source="deck", source_location="Tuvasa")
    second = writes.get_change(c, receiver)["linked_change_id"]
    assert writes.get_change(c, first)["status"] == "dropped"
    assert writes.get_change(c, second)["deck_id"] == _deck(c, "Tuvasa")

    writes.update_change(c, receiver, source=None, source_location=None)    # back to Auto
    assert writes.get_change(c, second)["status"] == "dropped"
    assert writes.get_change(c, receiver)["linked_change_id"] is None
    assert q.open_takes(c) == []

    writes.update_change(c, receiver, source="deck", source_location="Gisa")   # and onto a deck again
    assert [t["donor_deck_name"] for t in q.open_takes(c)] == ["Gisa"]


def test_a_take_quantity_edit_follows_to_the_donor_half_within_its_list(c):
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,2)", (_deck(c, "Gisa"), RING))
    c.commit()
    receiver = _staged(c)
    donor = writes.get_change(c, receiver)["linked_change_id"]
    writes.update_change(c, receiver, quantity=2)
    assert writes.get_change(c, donor)["quantity"] == 2
    with pytest.raises(ValueError, match="only runs"):
        writes.update_change(c, receiver, quantity=3)


def test_the_donor_half_cannot_be_sent_elsewhere(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    donor = writes.get_change(c, receiver)["linked_change_id"]
    with pytest.raises(ValueError, match="promised to Prosper"):
        writes.update_change(c, donor, dest_location="Lands Box")


def test_reopening_a_take_promises_the_copy_again_or_falls_back_to_auto(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    writes.drop_change(c, receiver)
    assert writes.reopen_change(c, receiver) == ["Gisa's copy is promised again."]
    assert [t["receiver_change_id"] for t in q.open_takes(c)] == [receiver]

    writes.drop_change(c, receiver)
    writes.remove_deck_card(c, _deck(c, "Gisa"), RING)       # Gisa no longer has it to give
    lines = writes.reopen_change(c, receiver)
    assert lines and lines[0].startswith("Brought back on Auto: Gisa doesn't run Sol Ring")
    row = writes.get_change(c, receiver)
    assert (row["status"], row["source"], row["linked_change_id"]) == ("idea", None, None)


def test_a_donor_half_cannot_be_reopened_on_its_own(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    donor = writes.get_change(c, receiver)["linked_change_id"]
    writes.drop_change(c, receiver)
    with pytest.raises(ValueError, match="promised to Prosper"):
        writes.reopen_change(c, donor)


def test_adding_the_card_by_hand_retires_the_take_and_its_promise(c):
    _gisa_runs_ring(c)
    writes.add_collection_lot(c, RING, quantity=1, location="Box")
    receiver = _staged(c)
    donor = writes.get_change(c, receiver)["linked_change_id"]
    writes.add_deck_card(c, _deck(c, "Prosper"), RING, 1)     # on Auto, from the box
    assert writes.get_change(c, receiver)["status"] == "dropped"
    assert writes.get_change(c, donor)["status"] == "dropped"
    assert _list(c, "Gisa") == 1


# ------------------------------------------------------------------
# Dismantles are pairs too
# ------------------------------------------------------------------
def test_stage_dismantle_links_its_pairs_and_sets_their_source(c):
    _gisa_runs_ring(c)
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (_deck(c, "Prosper"), RING))
    c.commit()
    staged = writes.stage_dismantle(c, _deck(c, "Gisa"), _deck(c, "Prosper"))
    (removed,), (added,) = staged["removed"], staged["added"]
    add_row, cut_row = writes.get_change(c, added), writes.get_change(c, removed)
    assert (add_row["source"], add_row["source_location"]) == ("deck", "Gisa")
    assert add_row["linked_change_id"] == removed and cut_row["linked_change_id"] == added

    results = writes.apply_changes(c, staged["removed"] + staged["added"])
    assert len(results) == 1 and results[0]["ok"]
    assert _lots(c) == [("Prosper", 1)]
    assert _list(c, "Gisa") == 0 and _list(c, "Prosper") == 1


# ------------------------------------------------------------------
# The allocator and conflicts
# ------------------------------------------------------------------
def test_a_take_claims_the_donors_copy_for_the_receiver(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    other = writes.add_change(c, _deck(c, "Tuvasa"), add_scryfall_id=RING)   # Auto, nothing in storage
    verdicts = q.plan_availability(c, q.open_changes_dataframe(c, ("idea", "planned")))
    assert verdicts[receiver]["verdict"] == "available"
    assert verdicts[receiver]["text"] == "From Gisa (your pick)"
    assert verdicts[other]["verdict"] == "claimed_by"
    assert [w["deck_name"] for w in verdicts[other]["claimed_by"]] == ["Prosper"]
    # A one-deck view sees the promise too: it is read from the database.
    only_tuvasa = q.plan_availability(c, q.open_changes_dataframe(c, ("idea",), deck_id=_deck(c, "Tuvasa")))
    assert only_tuvasa[other]["verdict"] == "claimed_by"


def test_a_take_from_an_unsleeved_deck_says_why(c):
    _gisa_runs_ring(c, sleeved=False)
    writes.add_collection_lot(c, RING, quantity=1, location="Box")
    receiver = _staged(c)
    verdict = q.plan_availability(c, q.open_changes_dataframe(c, ("idea",)))[receiver]
    assert verdict["verdict"] == "wrong_location"
    assert verdict["text"] == "None sleeved in Gisa — free in Box"


def test_a_lopsided_brew_promise_still_claims_gisas_copy(c):
    _gisa_runs_ring(c)
    _brew(c)
    writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 1)
    other = writes.add_change(c, _deck(c, "Tuvasa"), add_scryfall_id=RING)
    verdict = q.plan_availability(c, q.open_changes_dataframe(c, ("idea",)))[other]
    assert verdict["verdict"] == "claimed_by"
    assert verdict["text"] == "Only in other decks, promised to Prosper (from Gisa)"


def test_a_take_plus_a_row_served_from_storage_is_not_a_conflict(c):
    _gisa_runs_ring(c)
    writes.add_collection_lot(c, RING, quantity=1, location="Box")
    _staged(c)
    writes.add_change(c, _deck(c, "Tuvasa"), add_scryfall_id=RING)
    changes = q.open_changes_dataframe(c, ("idea", "planned"))
    assert q.plan_conflicts(c, changes) == []


# ------------------------------------------------------------------
# Delete / rename
# ------------------------------------------------------------------
def test_deleting_the_receiver_drops_the_promise_on_the_donor(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    donor = writes.get_change(c, receiver)["linked_change_id"]
    name, err, redirected, dropped = writes.delete_deck(c, _deck(c, "Prosper"))
    assert (err, redirected, dropped) == (None, 0, 1)
    row = writes.get_change(c, donor)
    assert row["status"] == "dropped" and row["linked_change_id"] is None
    assert row["dest_location"] == "Prosper"   # left as written: it's dropped, not redirected


def test_deleting_the_donor_drops_the_take_and_unlinks_history(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    # An applied pair from earlier, too: its link must not dangle.
    c.execute("INSERT INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,1)", (_deck(c, "Gisa"), BLADE))
    c.commit()
    writes.add_collection_lot(c, BLADE, quantity=1, location="Gisa")
    done = writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Tuvasa"), BLADE, 1)
    _n, _e, _r, dropped = writes.delete_deck(c, _deck(c, "Gisa"))
    assert dropped == 1
    assert writes.get_change(c, receiver)["status"] == "dropped"
    applied = writes.get_change(c, done["receiver_change_id"])
    assert applied["status"] == "applied" and applied["linked_change_id"] is None


def test_renaming_the_donor_keeps_the_take_pointed_at_it(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    assert writes.rename_deck(c, _deck(c, "Gisa"), "Gisa Reborn") == (True, None)
    assert writes.get_change(c, receiver)["source_location"] == "Gisa Reborn"
    writes.promote_change(c, receiver)
    writes.apply_change(c, receiver)
    assert _lots(c) == [("Prosper", 1)]


# ------------------------------------------------------------------
# Where it shows
# ------------------------------------------------------------------
def test_the_workbench_shows_a_pair_as_one_row_under_the_receiver(c):
    _gisa_runs_ring(c)
    receiver = _staged(c, status="planned")
    planned = q.open_changes_dataframe(c, ("planned",))
    assert len(planned) == 2
    (entry,) = q.collapse_linked_pairs(planned)
    assert (entry["group_deck_name"], entry["change_id"]) == ("Prosper", receiver)
    assert entry["pair"] == {"card": RING, "from_deck": "Gisa", "to_deck": "Prosper", "receiver_status": "planned"}


def test_the_workbench_still_shows_a_pair_whose_receiver_is_an_idea(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    donor = writes.get_change(c, receiver)["linked_change_id"]
    (entry,) = q.collapse_linked_pairs(q.open_changes_dataframe(c, ("planned",)))
    assert (entry["group_deck_name"], entry["change_id"]) == ("Prosper", donor)
    assert entry["pair"]["receiver_status"] == "idea"


def test_an_unlinked_row_passes_through(c):
    writes.add_change(c, _deck(c, "Gisa"), add_scryfall_id=SIGNET, remove_scryfall_id=BLADE,
                      status="planned")
    (entry,) = q.collapse_linked_pairs(q.open_changes_dataframe(c, ("planned",)))
    assert entry["pair"] is None and entry["group_deck_name"] == "Gisa"


def test_the_donors_board_badges_the_card_and_lists_it_as_leaving(c):
    _gisa_runs_ring(c)
    receiver = _staged(c)
    donor = writes.get_change(c, receiver)["linked_change_id"]
    assert q.deck_leaving(c, _deck(c, "Gisa")) == {RING: ["Prosper"]}
    (item,) = deck_board.load_board_leaving(c, _deck(c, "Gisa"))
    assert item == {"change_id": donor, "id": RING, "name": RING, "qty": 1, "to_deck": "Prosper",
                    "receiver_status": "idea"}
    cards = deck_board.board_payload(q.deck_cards_dataframe(c, _deck(c, "Gisa")), {}, {},
                                     leaving=q.deck_leaving(c, _deck(c, "Gisa")))
    assert cards[0]["leaving"] == ["Prosper"]


def test_the_receivers_board_offers_decks_and_reads_from_gisa(c):
    _gisa_runs_ring(c)
    _staged(c)
    (item,) = deck_board.load_board_shortlist(c, _deck(c, "Prosper"))
    assert (item["source"], item["source_location"]) == ("deck", "Gisa")
    assert item["deck_options"] == [{"deck": "Gisa", "label": "Deck: Gisa — 1 sleeved"}]
    assert item["verdict_text"] == "From Gisa (your pick)"


def test_board_add_from_a_deck_is_a_take_with_no_undo(c):
    _gisa_runs_ring(c)
    idea = writes.add_change(c, _deck(c, "Prosper"), add_scryfall_id=RING)
    result = deck_board.apply_board_action(
        c, _deck(c, "Prosper"), {"op": "add", "change_id": idea, "source": "deck", "source_location": "Gisa"})
    assert result["undo"] is None
    assert result["message"] == "Took Sol Ring from Gisa, off Gisa's list"
    assert [(m["from"], m["to"]) for m in result["moves"]] == [("Gisa", "Prosper")]
    assert _list(c, "Gisa") == 0 and writes.get_change(c, idea)["status"] == "dropped"


def test_board_apply_and_keep_here(c):
    _gisa_runs_ring(c)
    receiver = _staged(c, status="planned")
    donor = writes.get_change(c, receiver)["linked_change_id"]
    kept = deck_board.apply_board_action(c, _deck(c, "Gisa"), {"op": "drop", "change_id": donor})
    assert kept["undo"] is None
    assert kept["message"].startswith("Kept Sol Ring — the take is cancelled.")
    assert writes.get_change(c, receiver)["status"] == "dropped"

    receiver = _staged(c, status="planned")
    result = deck_board.apply_board_action(c, _deck(c, "Prosper"), {"op": "apply", "change_id": receiver})
    assert result["message"] == "Took Sol Ring from Gisa into Prosper."
    assert _lots(c) == [("Prosper", 1)]


def test_card_lookup_lists_pending_takes_and_history_reads_both_ways(c):
    _gisa_runs_ring(c)
    _staged(c)
    found = q.card_lookup(c, RING)
    assert found["pending_takes"] == [{"from_deck": "Gisa", "to_deck": "Prosper", "quantity": 1,
                                       "receiver_status": "idea"}]
    assert (found["shortlisted_in"][0]["source"], found["shortlisted_in"][0]["source_location"]) == ("deck", "Gisa")

    c.execute("DELETE FROM deck_changes")
    c.commit()
    writes.transfer_between_decks(c, _deck(c, "Gisa"), _deck(c, "Prosper"), RING, 1)
    routes = {h["deck_name"]: fmt.format_change_route(h["source"], h["source_location"], h["dest_location"])
              for h in q.card_lookup(c, RING)["history"]}
    assert routes == {"Gisa": "to Prosper", "Prosper": "from Gisa"}
