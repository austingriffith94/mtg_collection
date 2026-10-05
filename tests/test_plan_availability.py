"""The plan allocator — Phase 2 of the Workbench rework (see
IMPLEMENTATION_PLAN.md).

What's under test is the difference between asking "is this card
available?" once per shortlist row and asking it once for a whole queue.
The per-row answer (queries.card_inventory_status) is individually true
and collectively impossible: two planned swaps that each want your single
Blood Crypt are both told it's in storage. queries.plan_availability()
walks the queue in a fixed order and lets each row claim copies out of a
pool, so the second one is told who took it.

Also covers the oracle_id match that replaced matching on cards.name —
the bug that made a modal double-faced card's copies invisible to a
reference naming one face.
"""
import pytest

from dashboard_lib import queries as q


# ------------------------------------------------------------------
# Fixture helpers: a deck, a collection lot, a shortlist row.
# ------------------------------------------------------------------

def _deck(conn, name):
    cur = conn.execute("INSERT INTO decks (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def _lot(conn, scryfall_id, location=None, quantity=1):
    cur = conn.execute(
        "INSERT INTO collection (scryfall_id, quantity, location) VALUES (?,?,?)",
        (scryfall_id, quantity, location),
    )
    conn.commit()
    return cur.lastrowid


def _change(conn, deck_id, *, add_scryfall_id=None, add_name=None, remove_name="Something",
            quantity=1, status="planned", planned_at="2026-01-01 00:00:00", review_flag=0):
    cur = conn.execute(
        """INSERT INTO deck_changes
               (deck_id, status, add_scryfall_id, add_name, remove_name, quantity,
                review_flag, planned_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (deck_id, status, add_scryfall_id, add_name, remove_name, quantity,
         review_flag, planned_at if status == "planned" else None),
    )
    conn.commit()
    return cur.lastrowid


def _queue(conn):
    """The queue exactly as a page loads it, so the tests exercise the real
    column shape rather than hand-built dicts."""
    return q.open_changes_dataframe(conn, ("planned", "idea"))


# ------------------------------------------------------------------
# The core allocation: one copy, several claimants
# ------------------------------------------------------------------

def test_two_decks_wanting_one_copy_the_second_is_told_who_took_it(conn, add_card):
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box")
    raktres = _deck(conn, "Raktres")
    marchesa = _deck(conn, "Marchesa")
    first = _change(conn, raktres, add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    second = _change(conn, marchesa, add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[first]["verdict"] == "available"
    assert out[first]["locations"] == ["Box"]
    assert out[second]["verdict"] == "claimed_by"
    assert [c["change_id"] for c in out[second]["claimed_by"]] == [first]
    assert out[second]["claimed_by"][0]["deck_name"] == "Raktres"
    assert "Raktres" in out[second]["text"]


def test_two_rows_in_one_deck_wanting_one_copy_also_contend(conn, add_card):
    """The per-row check was wrong within a single deck too, not just
    across decks — which is why the Shortlist tab uses this function."""
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box")
    deck_id = _deck(conn, "Raktres")
    first = _change(conn, deck_id, add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    second = _change(conn, deck_id, add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[first]["verdict"] == "available"
    assert out[second]["verdict"] == "claimed_by"


def test_three_decks_wanting_two_owned_copies(conn, add_card):
    sid = add_card("Arcane Signet")
    _lot(conn, sid, location="Box", quantity=1)
    _lot(conn, sid, location="Lands Box", quantity=1)
    ids = [
        _change(conn, _deck(conn, f"Deck {n}"), add_scryfall_id=sid,
                planned_at=f"2026-01-0{n} 00:00:00")
        for n in (1, 2, 3)
    ]

    out = q.plan_availability(conn, _queue(conn))

    assert [out[i]["verdict"] for i in ids] == ["available", "available", "claimed_by"]
    # The two winners took different lots — not the same one twice.
    assert out[ids[0]]["lot_ids"] != out[ids[1]]["lot_ids"]
    assert len(out[ids[2]]["claimed_by"]) == 2


def test_one_lot_holding_several_copies_is_split_between_rows(conn, add_card):
    """Supply is copies, not lots: a single lot of 2 satisfies two rows."""
    sid = add_card("Swamp")
    lot_id = _lot(conn, sid, location="Lands Box", quantity=2)
    first = _change(conn, _deck(conn, "A"), add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    second = _change(conn, _deck(conn, "B"), add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")
    third = _change(conn, _deck(conn, "C"), add_scryfall_id=sid, planned_at="2026-01-03 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[first]["verdict"] == "available"
    assert out[second]["verdict"] == "available"
    assert out[first]["lot_ids"] == out[second]["lot_ids"] == [lot_id]
    assert out[third]["verdict"] == "claimed_by"


def test_a_row_wanting_more_copies_than_are_free_reports_the_shortfall(conn, add_card):
    sid = add_card("Swamp")
    _lot(conn, sid, location="Lands Box", quantity=1)
    change_id = _change(conn, _deck(conn, "A"), add_scryfall_id=sid, quantity=3)

    out = q.plan_availability(conn, _queue(conn))

    # Reported by the blocker for what it did NOT get, with the partial
    # claim kept so the caller can see how far it got.
    assert out[change_id]["verdict"] == "not_owned"
    assert (out[change_id]["claimed"], out[change_id]["wanted"]) == (1, 3)
    assert "got 1 of 3" in out[change_id]["text"]


# ------------------------------------------------------------------
# The other verdicts
# ------------------------------------------------------------------

def test_a_card_you_own_none_of_is_a_buy(conn, add_card):
    sid = add_card("Jeweled Lotus")
    change_id = _change(conn, _deck(conn, "A"), add_scryfall_id=sid)

    out = q.plan_availability(conn, _queue(conn))

    assert out[change_id]["verdict"] == "not_owned"
    assert out[change_id]["owned_qty"] == 0


def test_copies_sleeved_in_other_decks_are_an_unsleeve_not_a_buy(conn, add_card):
    sid = add_card("War Room")
    _lot(conn, sid, location="Gisa")          # a deck name = sleeved there
    _deck(conn, "Gisa")
    change_id = _change(conn, _deck(conn, "Raktres"), add_scryfall_id=sid)

    out = q.plan_availability(conn, _queue(conn))

    assert out[change_id]["verdict"] == "in_other_deck"
    assert out[change_id]["in_other_decks"] == {"Gisa": 1}
    assert out[change_id]["owned_qty"] == 1


def test_a_copy_already_sleeved_in_this_deck_claims_nothing(conn, add_card):
    """An idea for a card the deck already holds is a no-op, and must not
    eat the free copy some other deck could use."""
    sid = add_card("Sol Ring")
    gisa = _deck(conn, "Gisa")
    raktres = _deck(conn, "Raktres")
    _lot(conn, sid, location="Gisa")
    _lot(conn, sid, location="Box")
    stale = _change(conn, gisa, add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    other = _change(conn, raktres, add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[stale]["verdict"] == "already_here"
    assert out[stale]["claimed"] == 0
    # The storage copy went to the deck that actually needs it.
    assert out[other]["verdict"] == "available"


def test_a_removal_only_row_has_nothing_to_source(conn, add_card):
    sid = add_card("Lightning Bolt")
    deck_id = _deck(conn, "A")
    cur = conn.execute(
        "INSERT INTO deck_changes (deck_id, status, remove_scryfall_id) VALUES (?, 'planned', ?)",
        (deck_id, sid),
    )
    conn.commit()

    out = q.plan_availability(conn, _queue(conn))

    assert out[cur.lastrowid]["verdict"] == "no_add"


# ------------------------------------------------------------------
# Matching: oracle_id, not cards.name (the bug this phase fixed)
# ------------------------------------------------------------------

def test_an_mdfc_resolves_by_oracle_id_not_name(conn, add_card):
    """A modal double-faced card is stored under its full `Front // Back`
    name. A shortlist row naming one face used to match no collection row
    at all and report "not owned" over a card sitting in a box."""
    full = add_card("Malakir Rebirth // Malakir Mire", set_code="znr", collector_number="111")
    one_face = add_card("Malakir Rebirth", set_code="znr", collector_number="111b",
                        oracle_id="oracle-malakir rebirth // malakir mire")
    _lot(conn, full, location="Box")
    change_id = _change(conn, _deck(conn, "A"), add_scryfall_id=one_face)

    out = q.plan_availability(conn, _queue(conn))

    assert out[change_id]["verdict"] == "available"
    assert out[change_id]["owned_qty"] == 1


def test_two_rows_naming_different_faces_of_one_card_still_contend(conn, add_card):
    """The regression that matters: not just "it's found", but that two
    references to one physical card compete for it."""
    oracle = "oracle-witch enchanter // witch-blessed meadow"
    full = add_card("Witch Enchanter // Witch-Blessed Meadow", collector_number="1",
                    oracle_id=oracle)
    face = add_card("Witch Enchanter", collector_number="2", oracle_id=oracle)
    _lot(conn, full, location="Box")
    first = _change(conn, _deck(conn, "A"), add_scryfall_id=full, planned_at="2026-01-01 00:00:00")
    second = _change(conn, _deck(conn, "B"), add_scryfall_id=face, planned_at="2026-01-02 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[first]["verdict"] == "available"
    assert out[second]["verdict"] == "claimed_by"


def test_a_different_printing_of_the_same_card_is_the_same_card(conn, add_card):
    """Owning the cheap printing satisfies a row that linked the foil."""
    cheap = add_card("Cultivate", set_code="m21", collector_number="177")
    foil = add_card("Cultivate", set_code="ltr", collector_number="169")
    _lot(conn, cheap, location="Box")
    change_id = _change(conn, _deck(conn, "A"), add_scryfall_id=foil)

    out = q.plan_availability(conn, _queue(conn))

    assert out[change_id]["verdict"] == "available"


def test_an_unlinked_row_contends_with_a_linked_one(conn, add_card):
    """Every swap backfilled from the old queue stored only a typed name.
    It must still compete with a row that has a real printing linked, or
    the Workbench would promise one copy twice."""
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box")
    linked = _change(conn, _deck(conn, "A"), add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    typed = _change(conn, _deck(conn, "B"), add_name="blood crypt", planned_at="2026-01-02 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[linked]["verdict"] == "available"
    assert out[typed]["verdict"] == "claimed_by"


def test_a_card_with_no_oracle_id_falls_back_to_name_matching(conn, add_card):
    """A row added before oracle_id was synced still has to work."""
    sid = add_card("Old Import", oracle_id=False)
    _lot(conn, sid, location="Box")
    change_id = _change(conn, _deck(conn, "A"), add_scryfall_id=sid)

    out = q.plan_availability(conn, _queue(conn))

    assert out[change_id]["verdict"] == "available"


# ------------------------------------------------------------------
# Order
# ------------------------------------------------------------------

def test_allocation_order_is_planned_time_then_id_not_insertion_order(conn, add_card):
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box")
    late = _change(conn, _deck(conn, "Late"), add_scryfall_id=sid, planned_at="2026-06-01 00:00:00")
    early = _change(conn, _deck(conn, "Early"), add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[early]["verdict"] == "available"
    assert out[late]["verdict"] == "claimed_by"


def test_a_planned_row_outranks_an_idea_staged_later(conn, add_card):
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box")
    planned = _change(conn, _deck(conn, "A"), add_scryfall_id=sid,
                      status="planned", planned_at="2026-01-01 00:00:00")
    # An idea has no planned_at, so it sorts on created_at (now).
    idea = _change(conn, _deck(conn, "B"), add_scryfall_id=sid, status="idea")

    out = q.plan_availability(conn, _queue(conn))

    assert out[planned]["verdict"] == "available"
    assert out[idea]["verdict"] == "claimed_by"


def test_repeated_calls_give_identical_verdicts(conn, add_card):
    """The page reruns on every interaction. If allocation weren't
    deterministic, a contested card would change hands between reruns."""
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box")
    for n in (1, 2, 3):
        _change(conn, _deck(conn, f"Deck {n}"), add_scryfall_id=sid,
                planned_at="2026-01-01 00:00:00")

    runs = [
        {cid: v["verdict"] for cid, v in q.plan_availability(conn, _queue(conn)).items()}
        for _ in range(4)
    ]

    assert runs[1:] == runs[:-1]
    assert sorted(runs[0].values()) == ["available", "claimed_by", "claimed_by"]


def test_an_empty_queue_allocates_nothing(conn):
    assert q.plan_availability(conn, _queue(conn)) == {}
    assert q.plan_conflicts(conn, _queue(conn)) == []


# ------------------------------------------------------------------
# plan_conflicts
# ------------------------------------------------------------------

def test_a_contested_card_is_one_conflict_naming_every_wanter(conn, add_card):
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box")
    for n, deck in enumerate(("Raktres", "Marchesa", "Prosper"), start=1):
        _change(conn, _deck(conn, deck), add_scryfall_id=sid,
                planned_at=f"2026-01-0{n} 00:00:00")

    conflicts = q.plan_conflicts(conn, _queue(conn))

    assert len(conflicts) == 1
    only = conflicts[0]
    assert only["card"] == "Blood Crypt"
    assert (only["owned_qty"], only["available_qty"], only["wanted_qty"]) == (1, 1, 3)
    assert only["shortfall"] == 2
    assert [w["deck_name"] for w in only["wanters"]] == ["Raktres", "Marchesa", "Prosper"]


def test_one_row_wanting_an_unowned_card_is_not_a_conflict(conn, add_card):
    """That's a purchase, not a decision — it belongs on the buy list."""
    sid = add_card("Jeweled Lotus")
    _change(conn, _deck(conn, "A"), add_scryfall_id=sid)

    assert q.plan_conflicts(conn, _queue(conn)) == []


def test_enough_copies_for_everyone_is_not_a_conflict(conn, add_card):
    sid = add_card("Arcane Signet")
    _lot(conn, sid, location="Box", quantity=2)
    _change(conn, _deck(conn, "A"), add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    _change(conn, _deck(conn, "B"), add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")

    assert q.plan_conflicts(conn, _queue(conn)) == []


def test_several_rows_for_a_card_owned_only_in_other_decks_is_a_conflict(conn, add_card):
    """Nobody claimed it, so no row says claimed_by — but the two rows
    still can't both have it, and only one deck can give it up."""
    sid = add_card("War Room")
    _deck(conn, "Gisa")
    _lot(conn, sid, location="Gisa")
    _change(conn, _deck(conn, "A"), add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    _change(conn, _deck(conn, "B"), add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")

    conflicts = q.plan_conflicts(conn, _queue(conn))

    assert len(conflicts) == 1
    assert conflicts[0]["available_qty"] == 0
    assert [w["verdict"] for w in conflicts[0]["wanters"]] == ["in_other_deck", "in_other_deck"]


def test_an_already_here_row_is_not_counted_as_a_wanter(conn, add_card):
    sid = add_card("Sol Ring")
    gisa = _deck(conn, "Gisa")
    _lot(conn, sid, location="Gisa")
    _change(conn, gisa, add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    _change(conn, _deck(conn, "B"), add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")

    # One real wanter left, so there's nothing to decide between.
    assert q.plan_conflicts(conn, _queue(conn)) == []


def test_conflicts_are_ordered_by_shortfall(conn, add_card):
    small = add_card("Arcane Signet")
    big = add_card("Blood Crypt")
    _lot(conn, small, location="Box")
    _lot(conn, big, location="Box")
    for n in (1, 2):
        _change(conn, _deck(conn, f"S{n}"), add_scryfall_id=small,
                planned_at=f"2026-01-0{n} 00:00:00")
    for n in (3, 4, 5):
        _change(conn, _deck(conn, f"B{n}"), add_scryfall_id=big,
                planned_at=f"2026-01-0{n} 00:00:00")

    conflicts = q.plan_conflicts(conn, _queue(conn))

    assert [c["card"] for c in conflicts] == ["Blood Crypt", "Arcane Signet"]
    assert [c["shortfall"] for c in conflicts] == [2, 1]


def test_conflicts_reuses_a_passed_in_allocation(conn, add_card):
    """The page allocates once and passes the result to both renderers;
    the conflicts it shows must be the ones the table's verdicts describe."""
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box")
    for n in (1, 2):
        _change(conn, _deck(conn, f"D{n}"), add_scryfall_id=sid,
                planned_at=f"2026-01-0{n} 00:00:00")
    queue = _queue(conn)
    availability = q.plan_availability(conn, queue)

    assert q.plan_conflicts(conn, queue, availability) == q.plan_conflicts(conn, queue)


# ------------------------------------------------------------------
# card_inventory_status — the per-row check execute_swap still uses,
# now matching on oracle_id too
# ------------------------------------------------------------------

def test_card_inventory_status_finds_an_mdfc_by_either_name(conn, add_card):
    oracle = "oracle-malakir rebirth // malakir mire"
    full = add_card("Malakir Rebirth // Malakir Mire", collector_number="1", oracle_id=oracle)
    add_card("Malakir Rebirth", collector_number="2", oracle_id=oracle)
    _lot(conn, full, location="Box")

    by_face = q.card_inventory_status(conn, "Malakir Rebirth")
    by_full = q.card_inventory_status(conn, "Malakir Rebirth // Malakir Mire")

    assert by_face["owned_qty"] == by_full["owned_qty"] == 1
    assert len(by_face["available_lots"]) == 1


def test_card_inventory_status_prefers_a_given_printing_over_the_name(conn, add_card):
    sid = add_card("Cultivate", set_code="m21")
    other = add_card("Cultivate", set_code="ltr", collector_number="2")
    _lot(conn, other, location="Box")

    status = q.card_inventory_status(conn, "ignored text", scryfall_id=sid)

    assert status["owned_qty"] == 1


def test_card_inventory_status_is_empty_for_no_reference_at_all(conn):
    assert q.card_inventory_status(conn, "") == {
        "owned_qty": 0, "available_lots": [], "in_this_deck_qty": 0, "in_other_decks": {}
    }


# ------------------------------------------------------------------
# unlocated_lot_summary (the Workbench header, and Phase 4's input)
# ------------------------------------------------------------------

def test_unlocated_lot_summary_counts_only_lots_with_no_location(conn, add_card):
    sid = add_card("Sol Ring")
    conn.execute("UPDATE cards SET current_price_usd = 2.50 WHERE scryfall_id = ?", (sid,))
    _lot(conn, sid, location=None, quantity=2)
    _lot(conn, sid, location="   ", quantity=1)   # blank counts as unlocated
    _lot(conn, sid, location="Box", quantity=5)
    conn.commit()

    summary = q.unlocated_lot_summary(conn)

    assert summary["lots"] == 2
    assert summary["copies"] == 3
    assert summary["value"] == pytest.approx(7.50)


def test_unlocated_lot_summary_on_an_empty_collection(conn):
    assert q.unlocated_lot_summary(conn) == {"lots": 0, "copies": 0, "value": 0.0}


# ------------------------------------------------------------------
# open_changes_dataframe
# ------------------------------------------------------------------

def test_open_changes_dataframe_spans_decks_and_carries_the_deck_name(conn, add_card):
    sid = add_card("Sol Ring")
    a = _deck(conn, "Alpha")
    b = _deck(conn, "Beta")
    _change(conn, a, add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    _change(conn, b, add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")

    df = q.open_changes_dataframe(conn, ("planned",))

    assert list(df["deck_name"]) == ["Alpha", "Beta"]
    assert list(df["add_card"]) == ["Sol Ring", "Sol Ring"]
    assert list(df["oracle_id"]) == ["oracle-sol ring"] * 2


def test_open_changes_dataframe_can_narrow_to_one_deck(conn, add_card):
    sid = add_card("Sol Ring")
    a = _deck(conn, "Alpha")
    _change(conn, a, add_scryfall_id=sid)
    _change(conn, _deck(conn, "Beta"), add_scryfall_id=sid)

    df = q.open_changes_dataframe(conn, ("planned",), deck_id=a)

    assert list(df["deck_name"]) == ["Alpha"]


def test_open_changes_dataframe_excludes_resolved_rows(conn, add_card):
    sid = add_card("Sol Ring")
    deck_id = _deck(conn, "Alpha")
    _change(conn, deck_id, add_scryfall_id=sid, status="idea")
    conn.execute(
        "INSERT INTO deck_changes (deck_id, status, add_scryfall_id) VALUES (?, 'applied', ?)",
        (deck_id, sid),
    )
    conn.commit()

    assert len(q.open_changes_dataframe(conn, ("planned", "idea"))) == 1


def test_open_changes_dataframe_rejects_an_unknown_status(conn):
    with pytest.raises(ValueError):
        q.open_changes_dataframe(conn, ("maybe",))


# ------------------------------------------------------------------
# NULL quantity (collection.quantity is nullable: "untracked / bulk")
#
# 183 of the live database's 1,472 lots are NULL. Read as zero — which is
# what `quantity or 0` did — a card sitting in a box reports NOT OWNED,
# and every deck is free to claim the copy nobody counted.
# ------------------------------------------------------------------

def test_a_lot_with_no_quantity_counts_as_one_copy(conn, add_card):
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location=None, quantity=None)
    change_id = _change(conn, _deck(conn, "A"), add_scryfall_id=sid)

    out = q.plan_availability(conn, _queue(conn))

    assert out[change_id]["verdict"] == "available"
    assert out[change_id]["owned_qty"] == 1
    assert out[change_id]["locations"] == ["location unknown"]


def test_an_untracked_lot_can_only_be_claimed_once(conn, add_card):
    """The half that matters: counting it as 1 and not as unlimited."""
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box", quantity=None)
    first = _change(conn, _deck(conn, "A"), add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    second = _change(conn, _deck(conn, "B"), add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[first]["verdict"] == "available"
    assert out[second]["verdict"] == "claimed_by"


def test_card_inventory_status_counts_an_untracked_lot_as_owned(conn, add_card):
    sid = add_card("Blood Crypt")
    _lot(conn, sid, location="Box", quantity=None)

    status = q.card_inventory_status(conn, "Blood Crypt")

    assert status["owned_qty"] == 1
    assert len(status["available_lots"]) == 1


# ------------------------------------------------------------------
# Bulk basics: NULL quantity on a basic land means "untracked", i.e.
# unlimited — not the one copy a NULL lot counts as for other cards.
# ------------------------------------------------------------------

def _basic(conn, add_card, name="Mountain"):
    sid = add_card(name)
    conn.execute("UPDATE cards SET is_basic_land = 1 WHERE scryfall_id = ?", (sid,))
    conn.commit()
    return sid


def test_a_bulk_basic_is_available_to_every_deck_and_never_conflicts(conn, add_card):
    sid = _basic(conn, add_card)
    _lot(conn, sid, location="Lands Box", quantity=None)
    ids = [_change(conn, _deck(conn, f"D{n}"), add_scryfall_id=sid,
                   planned_at=f"2026-01-0{n} 00:00:00") for n in (1, 2, 3)]

    out = q.plan_availability(conn, _queue(conn))

    assert [out[i]["verdict"] for i in ids] == ["available"] * 3
    assert all(out[i]["bulk"] for i in ids)
    assert q.plan_conflicts(conn, _queue(conn), out) == []


def test_a_bulk_basic_does_not_starve_a_non_basic_with_a_null_lot(conn, add_card):
    """The unlimited rule is per card, not a blanket for NULL lots."""
    basic = _basic(conn, add_card)
    crypt = add_card("Blood Crypt")
    _lot(conn, basic, quantity=None)
    _lot(conn, crypt, location="Box", quantity=None)
    first = _change(conn, _deck(conn, "A"), add_scryfall_id=crypt, planned_at="2026-01-01 00:00:00")
    second = _change(conn, _deck(conn, "B"), add_scryfall_id=crypt, planned_at="2026-01-02 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[first]["verdict"] == "available" and not out[first].get("bulk")
    assert out[second]["verdict"] == "claimed_by"


def test_a_basic_with_tracked_quantities_is_still_limited(conn, add_card):
    sid = _basic(conn, add_card)
    _lot(conn, sid, location="Lands Box", quantity=1)
    first = _change(conn, _deck(conn, "A"), add_scryfall_id=sid, planned_at="2026-01-01 00:00:00")
    second = _change(conn, _deck(conn, "B"), add_scryfall_id=sid, planned_at="2026-01-02 00:00:00")

    out = q.plan_availability(conn, _queue(conn))

    assert out[first]["verdict"] == "available"
    assert out[second]["verdict"] == "claimed_by"
