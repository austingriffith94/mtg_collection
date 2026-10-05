"""writes.* for the deck_changes lifecycle (Workbench rework, Phase 1):
add / edit / promote / demote / drop / reopen / apply, plus the places the
old maybeboard and swap queue were read, which now read deck_changes
(prune_collection, Moxfield export, delete_deck, the dataframes the pages
use).

Schema creation and the backfill from the old tables are covered in
test_deck_changes.py; this file starts from an empty, current schema.
"""
import pytest

from dashboard_lib import moxfield_export, queries, writes


# ------------------------------------------------------------------
# Fixtures / helpers
# ------------------------------------------------------------------

@pytest.fixture
def deck(conn):
    cur = conn.execute("INSERT INTO decks (name) VALUES ('Gisa')")
    conn.commit()
    return cur.lastrowid


def _rows(conn, **where):
    sql = "SELECT * FROM deck_changes"
    if where:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
    sql += " ORDER BY change_id"
    return [dict(r) for r in conn.execute(sql, tuple(where.values())).fetchall()]


def _status(conn, change_id):
    return writes.get_change(conn, change_id)["status"]


def _lot(conn, scryfall_id):
    return dict(conn.execute(
        "SELECT * FROM collection WHERE scryfall_id=?", (scryfall_id,)
    ).fetchone())


def _in_deck(conn, deck_id, scryfall_id):
    return conn.execute(
        "SELECT 1 FROM deck_cards WHERE deck_id=? AND scryfall_id=?", (deck_id, scryfall_id)
    ).fetchone() is not None


@pytest.fixture
def swap_setup(conn, deck, add_card):
    """Gisa runs Heirloom Blade (sleeved in Gisa); Sol Ring sits in the Box.
    A planned swap Sol Ring <- Heirloom Blade is staged but not applied."""
    blade = add_card("Heirloom Blade")
    sol = add_card("Sol Ring")
    writes.add_deck_card(conn, deck, blade)
    writes.add_collection_lot(conn, blade, quantity=1, location="Gisa")
    writes.add_collection_lot(conn, sol, quantity=1, location="Box")
    change_id = writes.add_change(
        conn, deck, add_scryfall_id=sol, add_name="Sol Ring",
        remove_scryfall_id=blade, remove_name="Heirloom Blade", status="planned",
    )
    # The hand-edit above logged an 'applied' row for the mainboard add;
    # clear it so tests count only what they themselves cause.
    conn.execute("DELETE FROM deck_changes WHERE status='applied'")
    conn.commit()
    return {"deck": deck, "blade": blade, "sol": sol, "change": change_id}


# ------------------------------------------------------------------
# add_change
# ------------------------------------------------------------------

def test_add_change_defaults_to_an_unpaired_idea(conn, deck, add_card):
    sid = add_card("Sol Ring")
    cid = writes.add_change(conn, deck, add_scryfall_id=sid, add_name="Sol Ring", notes="staple")
    row = writes.get_change(conn, cid)
    assert row["status"] == "idea"
    assert row["remove_scryfall_id"] is None
    assert row["planned_at"] is None
    assert row["notes"] == "staple"
    assert row["review_flag"] == 0


def test_add_planned_requires_a_card_to_cut(conn, deck, add_card):
    sid = add_card("Sol Ring")
    with pytest.raises(ValueError, match="needs a card to replace"):
        writes.add_change(conn, deck, add_scryfall_id=sid, status="planned")


def test_add_planned_sets_planned_at(conn, deck, add_card):
    sol, blade = add_card("Sol Ring"), add_card("Heirloom Blade")
    cid = writes.add_change(conn, deck, add_scryfall_id=sol, remove_scryfall_id=blade, status="planned")
    assert writes.get_change(conn, cid)["planned_at"] is not None


def test_add_change_rejects_a_row_that_does_nothing(conn, deck):
    with pytest.raises(ValueError, match="add a card, remove one"):
        writes.add_change(conn, deck, notes="nothing here")


def test_add_change_refuses_a_duplicate_open_row(conn, deck, add_card):
    sid = add_card("Sol Ring")
    writes.add_change(conn, deck, add_scryfall_id=sid, add_name="Sol Ring")
    with pytest.raises(ValueError, match="already on this deck's shortlist"):
        writes.add_change(conn, deck, add_scryfall_id=sid, add_name="Sol Ring")


def test_a_dropped_row_does_not_block_re_adding_the_card(conn, deck, add_card):
    sid = add_card("Sol Ring")
    first = writes.add_change(conn, deck, add_scryfall_id=sid)
    writes.drop_change(conn, first)
    second = writes.add_change(conn, deck, add_scryfall_id=sid)
    assert second != first


def test_the_same_card_may_be_on_two_different_decks(conn, deck, add_card):
    other = conn.execute("INSERT INTO decks (name) VALUES ('Raktres')").lastrowid
    sid = add_card("Blood Crypt")
    writes.add_change(conn, deck, add_scryfall_id=sid)
    writes.add_change(conn, other, add_scryfall_id=sid)
    assert len(_rows(conn)) == 2


# ------------------------------------------------------------------
# promote / demote / drop / reopen
# ------------------------------------------------------------------

def test_promote_needs_a_target(conn, deck, add_card):
    cid = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"))
    with pytest.raises(ValueError, match="Pick a card"):
        writes.promote_change(conn, cid)
    assert _status(conn, cid) == "idea"


def test_promote_uses_the_ideas_own_replace_target(conn, deck, add_card):
    """The point of the rework: the target an idea already names is enough,
    nothing is retyped."""
    sol, blade = add_card("Sol Ring"), add_card("Heirloom Blade")
    cid = writes.add_change(
        conn, deck, add_scryfall_id=sol, remove_scryfall_id=blade, remove_name="heirloom blade"
    )
    writes.promote_change(conn, cid)
    row = writes.get_change(conn, cid)
    assert row["status"] == "planned"
    assert row["planned_at"] is not None
    assert row["remove_scryfall_id"] == blade
    # The lowercase text carried over from the old maybeboard is replaced by
    # the real card name now that there's a resolved id to read it from.
    assert row["remove_name"] == "Heirloom Blade"


def test_promote_with_an_explicit_target_overrides_the_stored_one(conn, deck, add_card):
    sol, blade, whisper = add_card("Sol Ring"), add_card("Heirloom Blade"), add_card("Night's Whisper")
    cid = writes.add_change(conn, deck, add_scryfall_id=sol, remove_scryfall_id=blade)
    writes.promote_change(conn, cid, remove_scryfall_id=whisper, quantity=2)
    row = writes.get_change(conn, cid)
    assert row["remove_scryfall_id"] == whisper
    assert row["remove_name"] == "Night's Whisper"
    assert row["quantity"] == 2


def test_promote_only_applies_to_ideas(conn, swap_setup):
    with pytest.raises(ValueError, match="'planned', not idea"):
        writes.promote_change(conn, swap_setup["change"])


def test_demote_keeps_the_pairing_and_clears_planned_at(conn, swap_setup):
    writes.demote_change(conn, swap_setup["change"])
    row = writes.get_change(conn, swap_setup["change"])
    assert row["status"] == "idea"
    assert row["planned_at"] is None
    assert row["remove_scryfall_id"] == swap_setup["blade"]


def test_drop_keeps_the_row_and_stamps_resolved_at(conn, deck, add_card):
    cid = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"))
    writes.drop_change(conn, cid)
    row = writes.get_change(conn, cid)
    assert row["status"] == "dropped"
    assert row["resolved_at"] is not None
    assert len(_rows(conn)) == 1  # kept, not deleted


def test_cannot_drop_something_already_applied(conn, swap_setup):
    writes.apply_change(conn, swap_setup["change"])
    with pytest.raises(ValueError):
        writes.drop_change(conn, swap_setup["change"])


def test_reopen_returns_a_dropped_idea(conn, deck, add_card):
    cid = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"))
    writes.drop_change(conn, cid)
    writes.reopen_change(conn, cid)
    row = writes.get_change(conn, cid)
    assert row["status"] == "idea"
    assert row["resolved_at"] is None


def test_reopen_refuses_when_the_card_is_back_on_the_shortlist(conn, deck, add_card):
    sid = add_card("Sol Ring")
    old = writes.add_change(conn, deck, add_scryfall_id=sid)
    writes.drop_change(conn, old)
    writes.add_change(conn, deck, add_scryfall_id=sid)
    with pytest.raises(ValueError, match="already back"):
        writes.reopen_change(conn, old)
    assert _status(conn, old) == "dropped"


# ------------------------------------------------------------------
# bulk_update_changes
# ------------------------------------------------------------------

def test_bulk_update_only_counts_real_differences(conn, deck, add_card):
    a = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"), notes="x")
    b = writes.add_change(conn, deck, add_scryfall_id=add_card("Arcane Signet"))
    changed = writes.bulk_update_changes(conn, deck, {
        a: {"notes": "x", "review_flag": False},   # identical to what's stored
        b: {"notes": "better", "review_flag": True},
    })
    assert changed == 1
    assert writes.get_change(conn, b)["notes"] == "better"
    assert writes.get_change(conn, b)["review_flag"] == 1


def test_bulk_update_skips_rows_no_longer_open(conn, deck, add_card):
    cid = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"))
    writes.drop_change(conn, cid)
    assert writes.bulk_update_changes(conn, deck, {cid: {"notes": "late edit"}}) == 0
    assert writes.get_change(conn, cid)["notes"] is None


def test_bulk_update_ignores_columns_it_does_not_own(conn, deck, add_card):
    cid = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"))
    writes.bulk_update_changes(conn, deck, {cid: {"status": "applied", "notes": "n"}})
    row = writes.get_change(conn, cid)
    assert row["status"] == "idea"  # status only moves through the transitions
    assert row["notes"] == "n"


# ------------------------------------------------------------------
# apply_change
# ------------------------------------------------------------------

def test_apply_swaps_the_deck_moves_the_lots_and_records_one_applied_row(conn, swap_setup):
    s = swap_setup
    outcome = writes.apply_change(conn, s["change"])

    assert not _in_deck(conn, s["deck"], s["blade"])
    assert _in_deck(conn, s["deck"], s["sol"])
    assert _lot(conn, s["blade"])["location"] == "Box"   # cut card returns to storage
    assert _lot(conn, s["sol"])["location"] == "Gisa"    # added card gets sleeved here
    assert outcome["moved_out"] and outcome["assigned_lot_id"] and not outcome["created_lot"]

    # All three effects, and exactly ONE history row: execute_swap goes
    # through the non-logging internals, so the swap isn't recorded again as
    # a separate add and remove.
    rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"
    assert rows[0]["resolved_at"] is not None
    assert rows[0]["add_name"] == "Sol Ring"
    assert rows[0]["remove_name"] == "Heirloom Blade"


def test_apply_creates_a_starter_lot_for_a_card_that_is_not_owned(conn, deck, add_card):
    blade, wanted = add_card("Heirloom Blade"), add_card("Jeweled Lotus")
    writes.add_deck_card(conn, deck, blade)
    cid = writes.add_change(
        conn, deck, add_scryfall_id=wanted, remove_scryfall_id=blade, status="planned"
    )
    outcome = writes.apply_change(conn, cid)
    assert outcome["created_lot"]
    assert _lot(conn, wanted)["location"] == "Gisa"
    assert _lot(conn, wanted)["source"] == "Auto-added (swap manager)"


def test_apply_refuses_when_the_cut_card_has_left_the_mainboard(conn, swap_setup):
    """Applying anyway would add a card without cutting one, leave the deck
    a card over, and record an 'applied' row claiming otherwise."""
    s = swap_setup
    writes.remove_deck_card(conn, s["deck"], s["blade"])
    conn.execute("DELETE FROM deck_changes WHERE status='applied'")
    conn.commit()

    with pytest.raises(ValueError, match="no longer in this deck's mainboard"):
        writes.apply_change(conn, s["change"])

    assert _status(conn, s["change"]) == "planned"
    assert not _in_deck(conn, s["deck"], s["sol"])   # nothing was written
    assert _lot(conn, s["sol"])["location"] == "Box"


def test_apply_only_works_on_planned_rows(conn, deck, add_card):
    cid = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"))
    with pytest.raises(ValueError, match="'idea', not planned"):
        writes.apply_change(conn, cid)


def test_apply_an_unlinked_swap_needs_a_printing_and_then_links_it(conn, deck, add_card):
    """Every swap backfilled from the old queue has only a typed name."""
    blade, sol = add_card("Heirloom Blade"), add_card("Sol Ring")
    writes.add_deck_card(conn, deck, blade)
    writes.add_collection_lot(conn, sol, quantity=1, location="Box")
    cid = writes.add_change(
        conn, deck, add_name="sol ring", remove_scryfall_id=blade, status="planned"
    )

    with pytest.raises(ValueError, match="isn't linked to a printing"):
        writes.apply_change(conn, cid)
    assert _status(conn, cid) == "planned"

    writes.apply_change(conn, cid, add_scryfall_id=sol)
    row = writes.get_change(conn, cid)
    assert row["status"] == "applied"
    assert row["add_scryfall_id"] == sol
    assert row["add_name"] == "Sol Ring"   # the real name, not the typed one


def test_apply_changes_runs_in_planned_order_and_one_failure_does_not_stop_the_rest(conn, deck, add_card):
    blade, whisper, medallion = add_card("Heirloom Blade"), add_card("Night's Whisper"), add_card("Jet Medallion")
    sol, ring = add_card("Sol Ring"), add_card("Arcane Signet")
    for sid in (blade, whisper, medallion):
        writes.add_deck_card(conn, deck, sid)

    first = writes.add_change(conn, deck, add_scryfall_id=sol, remove_scryfall_id=blade, status="planned")
    unresolvable = writes.add_change(conn, deck, add_name="Not A Real Card", remove_scryfall_id=whisper, status="planned")
    last = writes.add_change(conn, deck, add_scryfall_id=ring, remove_scryfall_id=medallion, status="planned")
    # Deliberately stamp planned_at out of insertion order.
    conn.execute("UPDATE deck_changes SET planned_at='2026-01-03 00:00:00' WHERE change_id=?", (first,))
    conn.execute("UPDATE deck_changes SET planned_at='2026-01-01 00:00:00' WHERE change_id=?", (last,))
    conn.execute("UPDATE deck_changes SET planned_at='2026-01-02 00:00:00' WHERE change_id=?", (unresolvable,))
    conn.commit()

    def resolver(name):
        return None, False, f"Couldn't find {name}"

    results = writes.apply_changes(conn, [first, unresolvable, last], resolve_add=resolver)

    assert [r["change_id"] for r in results] == [last, unresolvable, first]   # planned order
    assert [r["ok"] for r in results] == [True, False, True]
    assert "Couldn't find Not A Real Card" in results[1]["error"]
    assert _status(conn, unresolvable) == "planned"   # left staged, not half-applied
    assert _status(conn, first) == _status(conn, last) == "applied"


def test_apply_changes_links_an_unresolved_row_through_the_resolver(conn, deck, add_card):
    blade, sol = add_card("Heirloom Blade"), add_card("Sol Ring")
    writes.add_deck_card(conn, deck, blade)
    cid = writes.add_change(conn, deck, add_name="Sol Ring", remove_scryfall_id=blade, status="planned")

    results = writes.apply_changes(conn, [cid], resolve_add=lambda name: (sol, True, None))

    assert results[0]["ok"] and results[0]["was_new"]
    assert _in_deck(conn, deck, sol)


# ------------------------------------------------------------------
# Hand edits on the Mainboard tab are logged too
# ------------------------------------------------------------------

def test_adding_a_card_to_the_mainboard_logs_an_applied_row(conn, deck, add_card):
    sid = add_card("Sol Ring")
    writes.add_deck_card(conn, deck, sid, quantity=1)
    rows = _rows(conn)
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["add_scryfall_id"], rows[0]["add_name"]) == ("applied", sid, "Sol Ring")
    assert rows[0]["remove_scryfall_id"] is None
    assert rows[0]["resolved_at"] is not None


def test_removing_a_card_logs_what_was_removed_and_how_many(conn, deck, add_card):
    sid = add_card("Swamp")
    writes.add_deck_card(conn, deck, sid, quantity=5)
    writes.remove_deck_card(conn, deck, sid)
    removal = _rows(conn)[-1]
    assert removal["status"] == "applied"
    assert removal["remove_scryfall_id"] == sid
    assert removal["quantity"] == 5


def test_a_quantity_bump_is_not_logged_as_an_add(conn, deck, add_card):
    sid = add_card("Swamp")
    writes.add_deck_card(conn, deck, sid, quantity=4)
    writes.add_deck_card(conn, deck, sid, quantity=5)
    assert len(_rows(conn)) == 1  # only the first appearance on the list


def test_removing_a_card_that_was_never_there_logs_nothing(conn, deck, add_card):
    writes.remove_deck_card(conn, deck, add_card("Sol Ring"))
    assert _rows(conn) == []


def test_bulk_update_deck_cards_logs_through_the_same_path(conn, deck, add_card):
    a, b = add_card("Sol Ring"), add_card("Arcane Signet")
    writes.add_deck_card(conn, deck, a)
    conn.execute("DELETE FROM deck_changes")
    conn.commit()

    writes.bulk_update_deck_cards(conn, deck, {a: 0, b: 1})   # drop A, add B
    kinds = sorted((r["add_name"] or "", r["remove_name"] or "") for r in _rows(conn))
    assert kinds == [("", "Sol Ring"), ("Arcane Signet", "")]


# ------------------------------------------------------------------
# prune_collection must protect open shortlist cards — and survive NULLs
# ------------------------------------------------------------------

def _orphan_lot(conn, sid):
    """A lot prune_collection would delete if nothing protected it: no
    location, no quantity, not in any mainboard."""
    return writes.add_collection_lot(conn, sid, quantity=None, location=None)


@pytest.mark.parametrize("status", ["idea", "planned"])
def test_prune_keeps_a_lot_an_open_change_depends_on(conn, deck, add_card, status):
    keep, blade = add_card("Sol Ring"), add_card("Heirloom Blade")
    _orphan_lot(conn, keep)
    writes.add_change(
        conn, deck, add_scryfall_id=keep, remove_scryfall_id=blade if status == "planned" else None,
        status=status,
    )
    assert writes.prune_collection(conn) == []


def test_prune_deletes_a_lot_once_its_idea_is_dropped(conn, deck, add_card):
    sid = add_card("Sol Ring")
    _orphan_lot(conn, sid)
    cid = writes.add_change(conn, deck, add_scryfall_id=sid)
    writes.drop_change(conn, cid)
    assert [name for _, name in writes.prune_collection(conn)] == ["Sol Ring"]


def test_an_unlinked_change_does_not_switch_pruning_off(conn, deck, add_card):
    """add_scryfall_id is NULL for every swap backfilled from the old queue.
    `x NOT IN (subquery containing NULL)` is NULL rather than true, so
    without the IS NOT NULL filter one such row would silently stop
    prune_collection from ever deleting anything."""
    doomed, blade = add_card("Sol Ring"), add_card("Heirloom Blade")
    _orphan_lot(conn, doomed)
    writes.add_change(conn, deck, add_name="Some Typed Name", remove_scryfall_id=blade, status="planned")
    assert [name for _, name in writes.prune_collection(conn)] == ["Sol Ring"]


# ------------------------------------------------------------------
# Moxfield export
# ------------------------------------------------------------------

def _deck_with_shortlist(conn, deck, add_card):
    main = add_card("Command Tower", "cmd", "10")
    idea = add_card("Blood Crypt", "rtr", "1")
    staged = add_card("Sol Ring", "cmd", "2")
    cut = add_card("Heirloom Blade", "cmd", "3")
    dropped = add_card("Jeweled Lotus", "cmd", "4")
    unlinked_planned = None
    writes.add_deck_card(conn, deck, main)
    writes.add_deck_card(conn, deck, cut)
    writes.add_change(conn, deck, add_scryfall_id=idea)
    writes.add_change(conn, deck, add_scryfall_id=staged, remove_scryfall_id=cut, quantity=2, status="planned")
    writes.drop_change(conn, writes.add_change(conn, deck, add_scryfall_id=dropped))
    writes.add_change(conn, deck, add_name="Unlinked Card", remove_scryfall_id=cut, status="planned")
    return unlinked_planned


def test_export_puts_ideas_and_staged_swaps_under_maybeboard(conn, deck, add_card):
    _deck_with_shortlist(conn, deck, add_card)
    lines = moxfield_export.export_deck_lines(conn, deck, include_maybeboard=True)

    assert lines[lines.index("// Maybeboard") + 1:] == [
        "1 Blood Crypt (RTR) 1",
        "2 Sol Ring (CMD) 2",       # staged swap keeps its quantity
    ]


def test_export_leaves_out_dropped_applied_and_unlinked_rows(conn, deck, add_card):
    _deck_with_shortlist(conn, deck, add_card)
    text = moxfield_export.export_deck_text(conn, deck, include_maybeboard=True)
    assert "Jeweled Lotus" not in text      # dropped
    assert "Unlinked Card" not in text      # no printing to write a line for
    # The mainboard add logged an 'applied' row for Command Tower; it must
    # appear once (mainboard), not again under Maybeboard.
    assert text.count("Command Tower") == 1


def test_export_without_maybeboard_is_just_the_mainboard(conn, deck, add_card):
    _deck_with_shortlist(conn, deck, add_card)
    lines = moxfield_export.export_deck_lines(conn, deck, include_maybeboard=False)
    assert "// Maybeboard" not in lines
    assert not any("Blood Crypt" in line for line in lines)


# ------------------------------------------------------------------
# delete_deck
# ------------------------------------------------------------------

def test_deleting_a_deck_removes_its_changes_and_the_frozen_legacy_rows(conn, deck, add_card):
    """maybeboard / deck_swap_queue are frozen backups nothing writes to any
    more, but their rows still carry a deck_id FK — leaving them out of the
    delete would fail it under foreign_keys=ON."""
    sid, blade = add_card("Sol Ring"), add_card("Heirloom Blade")
    writes.add_change(conn, deck, add_scryfall_id=sid)
    conn.execute("INSERT INTO maybeboard (deck_id, scryfall_id) VALUES (?, ?)", (deck, sid))
    conn.execute(
        "INSERT INTO deck_swap_queue (deck_id, add_name, remove_scryfall_id, remove_name) VALUES (?,?,?,?)",
        (deck, "Sol Ring", blade, "Heirloom Blade"),
    )
    conn.commit()

    name, err = writes.delete_deck(conn, deck)

    assert err is None and name == "Gisa"
    assert conn.execute("SELECT COUNT(*) FROM deck_changes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM maybeboard").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM deck_swap_queue").fetchone()[0] == 0


# ------------------------------------------------------------------
# queries — what the pages read
# ------------------------------------------------------------------

def test_dataframe_prefers_the_real_card_name_over_stored_text(conn, deck, add_card):
    """Every backfilled maybeboard replace target was lowercase text that
    still resolved to a real card; the page should show the card's name."""
    sol, blade = add_card("Sol Ring"), add_card("Heirloom Blade")
    writes.add_change(conn, deck, add_scryfall_id=sol, add_name="sol ring",
                      remove_scryfall_id=blade, remove_name="heirloom blade")
    df = queries.deck_changes_dataframe(conn, deck, ("idea",))
    assert df.loc[0, "add_card"] == "Sol Ring"
    assert df.loc[0, "remove_card"] == "Heirloom Blade"


def test_dataframe_keeps_unlinked_rows_and_falls_back_to_a_known_price(conn, deck, add_card):
    other_printing = add_card("Sol Ring", "cmd", "99")
    conn.execute("UPDATE cards SET current_price_usd=1.5 WHERE scryfall_id=?", (other_printing,))
    blade = add_card("Heirloom Blade")
    conn.commit()
    writes.add_change(conn, deck, add_name="sol ring", remove_scryfall_id=blade, status="planned")

    df = queries.deck_changes_dataframe(conn, deck, ("planned",))
    assert len(df) == 1
    assert df.loc[0, "add_card"] == "sol ring"        # nothing better to show
    assert df.loc[0, "price"] == 1.5                  # borrowed from a printing with the same name


def test_dataframe_filters_by_status_and_rejects_unknown_ones(conn, deck, add_card):
    a = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"))
    writes.drop_change(conn, writes.add_change(conn, deck, add_scryfall_id=add_card("Arcane Signet")))
    assert list(queries.deck_changes_dataframe(conn, deck, ("idea",))["change_id"]) == [a]
    assert len(queries.deck_changes_dataframe(conn, deck, ("dropped",))) == 1
    assert queries.deck_changes_dataframe(conn, deck, ()).empty
    with pytest.raises(ValueError, match="Unknown change status"):
        queries.deck_changes_dataframe(conn, deck, ("idea; DROP TABLE decks",))


def test_shortlist_dataframe_is_ideas_and_planned_with_a_printing(conn, deck, add_card):
    idea, staged, dropped = add_card("Blood Crypt"), add_card("Sol Ring"), add_card("Jeweled Lotus")
    cut = add_card("Heirloom Blade")
    writes.add_change(conn, deck, add_scryfall_id=idea)
    writes.add_change(conn, deck, add_scryfall_id=staged, remove_scryfall_id=cut, status="planned")
    writes.drop_change(conn, writes.add_change(conn, deck, add_scryfall_id=dropped))
    writes.add_change(conn, deck, add_name="Unlinked", remove_scryfall_id=cut, status="planned")

    df = queries.shortlist_dataframe(conn, deck)
    assert sorted(df["name"]) == ["Blood Crypt", "Sol Ring"]
    assert set(df["status"]) == {"idea", "planned"}
    assert df.set_index("name").loc["Sol Ring", "replace_target"] == "Heirloom Blade"


def test_dashboard_summary_counts_ideas_and_planned_separately(conn, deck, add_card):
    cut = add_card("Heirloom Blade")
    writes.add_change(conn, deck, add_scryfall_id=add_card("Blood Crypt"))
    writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"), remove_scryfall_id=cut, status="planned")
    writes.drop_change(conn, writes.add_change(conn, deck, add_scryfall_id=add_card("Jeweled Lotus")))
    summary = queries.dashboard_summary(conn)
    assert (summary["idea_rows"], summary["planned_rows"]) == (1, 1)


# ------------------------------------------------------------------
# Auto-dropping a stale idea (Phase 2)
#
# An idea whose card is later added to the mainboard by hand used to sit
# on the shortlist forever, and the Workbench counted work already done.
# add_deck_card() now retires it, with a note so History can tell "I
# decided against this" from "I just did it by hand".
# ------------------------------------------------------------------

def test_hand_adding_a_card_auto_drops_the_idea_that_asked_for_it(conn, deck, add_card):
    sol = add_card("Sol Ring")
    change_id = writes.add_change(conn, deck, add_scryfall_id=sol, add_name="Sol Ring")

    writes.add_deck_card(conn, deck, sol)

    row = writes.get_change(conn, change_id)
    assert row["status"] == "dropped"
    assert row["resolved_at"] is not None
    assert writes.AUTO_DROP_NOTE in row["notes"]
    # The hand edit is still its own history row — one event, one row.
    applied = _rows(conn, status="applied")
    assert [r["add_scryfall_id"] for r in applied] == [sol]


def test_auto_drop_keeps_an_existing_note(conn, deck, add_card):
    sol = add_card("Sol Ring")
    change_id = writes.add_change(conn, deck, add_scryfall_id=sol, notes="cheap upgrade")

    writes.add_deck_card(conn, deck, sol)

    notes = writes.get_change(conn, change_id)["notes"]
    assert notes.startswith("cheap upgrade")
    assert writes.AUTO_DROP_NOTE in notes


def test_auto_drop_also_retires_a_planned_swap_for_the_same_card(conn, deck, add_card):
    sol, blade = add_card("Sol Ring"), add_card("Heirloom Blade")
    writes.add_deck_card(conn, deck, blade)
    change_id = writes.add_change(conn, deck, add_scryfall_id=sol,
                                  remove_scryfall_id=blade, status="planned")

    writes.add_deck_card(conn, deck, sol)

    assert _status(conn, change_id) == "dropped"


def test_auto_drop_matches_across_printings(conn, deck, add_card):
    """An idea for the cheap printing is satisfied by hand-adding the foil."""
    cheap = add_card("Cultivate", set_code="m21", collector_number="177")
    foil = add_card("Cultivate", set_code="ltr", collector_number="169")
    change_id = writes.add_change(conn, deck, add_scryfall_id=cheap)

    writes.add_deck_card(conn, deck, foil)

    assert _status(conn, change_id) == "dropped"


def test_auto_drop_matches_an_unlinked_row_by_name(conn, deck, add_card):
    """Every swap backfilled from the old queue stored only a typed name."""
    sol = add_card("Sol Ring", oracle_id=False)
    change_id = writes.add_change(conn, deck, add_name="sol ring")

    writes.add_deck_card(conn, deck, sol)

    assert _status(conn, change_id) == "dropped"


def test_auto_drop_leaves_other_decks_shortlists_alone(conn, deck, add_card):
    sol = add_card("Sol Ring")
    other = conn.execute("INSERT INTO decks (name) VALUES ('Raktres')").lastrowid
    conn.commit()
    mine = writes.add_change(conn, deck, add_scryfall_id=sol)
    theirs = writes.add_change(conn, other, add_scryfall_id=sol)

    writes.add_deck_card(conn, deck, sol)

    assert _status(conn, mine) == "dropped"
    assert _status(conn, theirs) == "idea"


def test_a_quantity_bump_does_not_auto_drop_anything(conn, deck, add_card):
    """Only a genuine join retires an idea — matching the Phase 1 rule that
    a quantity change isn't a history event either."""
    swamp = add_card("Swamp")
    writes.add_deck_card(conn, deck, swamp, quantity=4)
    change_id = writes.add_change(conn, deck, add_scryfall_id=swamp)

    writes.add_deck_card(conn, deck, swamp, quantity=5)

    assert _status(conn, change_id) == "idea"


def test_applying_a_swap_does_not_auto_drop_its_own_row(conn, swap_setup):
    """execute_swap() goes through the non-logging _put_deck_card, so an
    applied swap stays 'applied' — not overwritten as 'dropped'."""
    writes.apply_change(conn, swap_setup["change"])

    assert _status(conn, swap_setup["change"]) == "applied"
    assert writes.get_change(conn, swap_setup["change"])["notes"] is None


def test_an_auto_dropped_idea_can_be_restored(conn, deck, add_card):
    """The escape hatch: if the hand-add was the mistake, the idea comes
    back rather than being gone."""
    sol = add_card("Sol Ring")
    change_id = writes.add_change(conn, deck, add_scryfall_id=sol)
    writes.add_deck_card(conn, deck, sol)

    writes.reopen_change(conn, change_id)

    assert _status(conn, change_id) == "idea"


def test_auto_drop_on_a_deck_with_no_shortlist_is_a_no_op(conn, deck, add_card):
    sol = add_card("Sol Ring")
    writes.add_deck_card(conn, deck, sol)
    assert _rows(conn, status="dropped") == []


# ------------------------------------------------------------------
# resolve_contention (Phase 2) — the Workbench's "several decks want this
# one card" decision. The rule lives here rather than in the page so it's
# testable: st.data_editor checkbox state can't be driven from AppTest.
# ------------------------------------------------------------------

@pytest.fixture
def contention(conn, add_card):
    """Three decks, each with an open idea for the one Blood Crypt.
    Keyed A/B/C; the names avoid the `deck` fixture's own deck, which some
    of these tests also use for a row that ISN'T competing."""
    sol = add_card("Blood Crypt")
    out = {}
    for name in ("Contender A", "Contender B", "Contender C"):
        deck_id = conn.execute("INSERT INTO decks (name) VALUES (?)", (name,)).lastrowid
        out[name[-1]] = writes.add_change(conn, deck_id, add_scryfall_id=sol, add_name="Blood Crypt")
    conn.commit()
    return out


def test_keeping_one_row_drops_every_other_wanter(conn, contention):
    outcome = writes.resolve_contention(
        conn, list(contention.values()), keep_change_id=contention["B"])

    assert outcome["kept"] == contention["B"]
    assert sorted(outcome["dropped"]) == sorted(
        [contention["A"], contention["C"]])
    assert outcome["errors"] == []
    assert _status(conn, contention["B"]) == "idea"
    assert _status(conn, contention["A"]) == "dropped"
    assert _status(conn, contention["C"]) == "dropped"


def test_dropping_named_rows_without_picking_a_winner(conn, contention):
    outcome = writes.resolve_contention(
        conn, list(contention.values()), drop_change_ids=[contention["A"]])

    assert outcome["kept"] is None
    assert outcome["dropped"] == [contention["A"]]
    # The other two are left to fight it out another day.
    assert _status(conn, contention["B"]) == "idea"
    assert _status(conn, contention["C"]) == "idea"


def test_a_keep_and_an_unrelated_drop_combine(conn, contention):
    outcome = writes.resolve_contention(
        conn, list(contention.values()),
        keep_change_id=contention["A"], drop_change_ids=[contention["C"]])

    assert outcome["kept"] == contention["A"]
    # Marchesa was named once and implied once — dropped once.
    assert sorted(outcome["dropped"]) == sorted([contention["C"], contention["B"]])
    assert _status(conn, contention["A"]) == "idea"


def test_resolving_nothing_drops_nothing(conn, contention):
    """A stray click can't quietly clear a conflict."""
    outcome = writes.resolve_contention(conn, list(contention.values()))

    assert outcome == {"kept": None, "dropped": [], "errors": []}
    assert all(_status(conn, cid) == "idea" for cid in contention.values())


def test_keeping_and_dropping_the_same_row_is_refused(conn, contention):
    with pytest.raises(ValueError, match="both keep and drop"):
        writes.resolve_contention(
            conn, list(contention.values()),
            keep_change_id=contention["A"], drop_change_ids=[contention["A"]])
    assert all(_status(conn, cid) == "idea" for cid in contention.values())


def test_keeping_a_row_that_isnt_competing_is_refused(conn, contention, deck, add_card):
    stranger = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"))

    with pytest.raises(ValueError, match="isn't one of the changes"):
        writes.resolve_contention(conn, list(contention.values()), keep_change_id=stranger)
    assert all(_status(conn, cid) == "idea" for cid in contention.values())


def test_dropping_a_row_that_isnt_competing_is_refused(conn, contention, deck, add_card):
    stranger = writes.add_change(conn, deck, add_scryfall_id=add_card("Sol Ring"))

    with pytest.raises(ValueError, match="Not competing"):
        writes.resolve_contention(conn, list(contention.values()), drop_change_ids=[stranger])
    assert _status(conn, stranger) == "idea"


def test_a_row_resolved_elsewhere_is_reported_not_raised(conn, contention):
    """The page renders, you apply the row in another tab, then you press
    Resolve. The rest must still resolve, with the stale one explained."""
    writes.drop_change(conn, contention["A"])

    outcome = writes.resolve_contention(
        conn, list(contention.values()), keep_change_id=contention["B"])

    assert outcome["dropped"] == [contention["C"]]
    assert [cid for cid, _msg in outcome["errors"]] == [contention["A"]]
    assert _status(conn, contention["B"]) == "idea"
