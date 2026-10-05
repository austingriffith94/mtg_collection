"""deck_changes lifecycle table — Phase 0 of the Workbench rework (see
IMPLEMENTATION_PLAN.md).

Covers the additive-upgrade path that creates the table on a database
built before it existed, and the one-time backfill that folds the
superseded maybeboard / deck_swap_queue rows into it without anything
having to be retyped.

Note on the fixture: conftest's `conn` builds from schema.sql, which now
includes deck_changes, so the table is already there and the seed
function never fires. Tests that exercise creation/backfill therefore
DROP the table first and re-run ensure_schema() — which is the real
upgrade path a pre-rework database takes.
"""
import sqlite3

import pytest

from dashboard_lib import queries


def _deck(conn, name="Test Deck"):
    cur = conn.execute("INSERT INTO decks (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid


def _rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM deck_changes ORDER BY change_id"
    ).fetchall()]


# ------------------------------------------------------------------
# Schema creation
# ------------------------------------------------------------------

def test_ensure_schema_creates_table_and_index(conn):
    conn.execute("DROP TABLE deck_changes")
    conn.commit()
    assert "deck_changes" not in queries._existing_objects(conn, "table")

    queries.ensure_schema(conn)

    assert "deck_changes" in queries._existing_objects(conn, "table")
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_deck_changes_deck_status" in indexes


def test_row_must_add_or_remove_something(conn):
    deck_id = _deck(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO deck_changes (deck_id, status, notes) VALUES (?, 'idea', ?)",
            (deck_id, "a thought about nothing in particular"),
        )


def test_status_is_constrained(conn):
    deck_id = _deck(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO deck_changes (deck_id, status, add_name) VALUES (?, 'maybe', ?)",
            (deck_id, "Sol Ring"),
        )


def test_status_defaults_to_idea(conn):
    deck_id = _deck(conn)
    conn.execute(
        "INSERT INTO deck_changes (deck_id, add_name) VALUES (?, ?)",
        (deck_id, "Sol Ring"),
    )
    conn.commit()
    assert _rows(conn)[0]["status"] == "idea"


def test_unowned_unpaired_idea_is_allowed(conn):
    """A brew/wishlist entry: no remove target, no owned copy, name only."""
    deck_id = _deck(conn)
    conn.execute(
        "INSERT INTO deck_changes (deck_id, add_name, notes) VALUES (?, ?, ?)",
        (deck_id, "Jeweled Lotus", "pricey, but worth costing out"),
    )
    conn.commit()
    row = _rows(conn)[0]
    assert row["add_scryfall_id"] is None
    assert row["remove_scryfall_id"] is None
    assert row["status"] == "idea"


# ------------------------------------------------------------------
# Backfill from the superseded tables
# ------------------------------------------------------------------

@pytest.fixture
def legacy(conn, add_card):
    """A pre-rework database: maybeboard + deck_swap_queue populated, and
    deck_changes dropped so ensure_schema() takes the upgrade path."""
    deck_id = _deck(conn, "Gisa, Zombie Gardener")
    sol = add_card("Sol Ring")
    blade = add_card("Heirloom Blade")
    whisper = add_card("Night's Whisper")

    # An idea with a resolved replace target, flagged for review.
    conn.execute(
        """INSERT INTO maybeboard
           (deck_id, scryfall_id, review_flag, replace_scryfall_id,
            replace_card_name, notes)
           VALUES (?, ?, 1, ?, ?, ?)""",
        (deck_id, sol, blade, "Heirloom Blade", "obvious upgrade"),
    )
    # An unpaired idea with nothing but a card.
    conn.execute(
        "INSERT INTO maybeboard (deck_id, scryfall_id) VALUES (?, ?)",
        (deck_id, whisper),
    )
    # A staged swap.
    conn.execute(
        """INSERT INTO deck_swap_queue
           (deck_id, add_name, remove_scryfall_id, remove_name, quantity, queued_at)
           VALUES (?, ?, ?, ?, 2, '2026-10-03 18:44:36')""",
        (deck_id, "Undead Warchief", blade, "Heirloom Blade"),
    )
    conn.execute("DROP TABLE deck_changes")
    conn.commit()
    return {"deck_id": deck_id, "sol": sol, "blade": blade, "whisper": whisper}


def test_backfill_maps_maybeboard_to_ideas(conn, legacy):
    queries.ensure_schema(conn)
    ideas = [r for r in _rows(conn) if r["status"] == "idea"]
    assert len(ideas) == 2

    paired = next(r for r in ideas if r["add_scryfall_id"] == legacy["sol"])
    assert paired["add_name"] == "Sol Ring"
    assert paired["review_flag"] == 1
    assert paired["notes"] == "obvious upgrade"
    # The replace target carries over, but the row stays an idea — it was
    # never staged, so promoting it would be a surprise.
    assert paired["remove_scryfall_id"] == legacy["blade"]
    assert paired["remove_name"] == "Heirloom Blade"
    assert paired["status"] == "idea"
    assert paired["planned_at"] is None

    unpaired = next(r for r in ideas if r["add_scryfall_id"] == legacy["whisper"])
    assert unpaired["remove_scryfall_id"] is None
    assert unpaired["review_flag"] == 0


def test_backfill_maps_swap_queue_to_planned(conn, legacy):
    queries.ensure_schema(conn)
    planned = [r for r in _rows(conn) if r["status"] == "planned"]
    assert len(planned) == 1

    row = planned[0]
    assert row["add_name"] == "Undead Warchief"
    assert row["remove_scryfall_id"] == legacy["blade"]
    assert row["quantity"] == 2
    # The old queue had no unpaired state, so a queued row was born planned.
    assert row["created_at"] == "2026-10-03 18:44:36"
    assert row["planned_at"] == "2026-10-03 18:44:36"
    assert row["resolved_at"] is None


def test_backfill_runs_once_and_is_not_repeated(conn, legacy):
    queries.ensure_schema(conn)
    before = _rows(conn)
    assert len(before) == 3

    # Every subsequent connect re-runs ensure_schema; it must be a no-op.
    queries.ensure_schema(conn)
    queries.ensure_schema(conn)
    assert _rows(conn) == before


def test_backfill_tolerates_missing_legacy_tables(conn):
    """A database that predates deck_swap_queue entirely still upgrades."""
    conn.execute("DROP TABLE deck_changes")
    conn.execute("DROP TABLE deck_swap_queue")
    conn.execute("DROP TABLE maybeboard")
    conn.commit()

    queries.ensure_schema(conn)

    assert "deck_changes" in queries._existing_objects(conn, "table")
    assert _rows(conn) == []


def test_backfill_on_empty_legacy_tables_adds_nothing(conn):
    conn.execute("DROP TABLE deck_changes")
    conn.commit()
    queries.ensure_schema(conn)
    assert _rows(conn) == []
