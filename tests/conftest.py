"""Shared pytest fixtures: a throwaway in-memory database built from
schema.sql (same open_connection path the app uses, so additive schema
upgrades are applied too). Never touches mtg_collection.db."""
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dashboard_lib import queries  # noqa: E402


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(PROJECT_ROOT, "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    queries.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def add_card(conn):
    """add_card('Sol Ring') -> scryfall_id of a minimal card row.

    `oracle_id` defaults to one derived from the name, so two printings
    added under the same name group together the way real Scryfall data
    does — that grouping is the join key the inventory allocator uses
    (queries.oracle_ids_for_card). Pass it explicitly to model the cases
    where name and oracle identity come apart: two printings of one card
    whose names differ (a modal double-faced card referred to by one
    face), or two genuinely different cards sharing a name. Pass
    oracle_id=False for a card with no oracle_id at all, which is what a
    row added before the field was synced looks like."""
    def _add(name, set_code="cmd", collector_number="1", oracle_id=None):
        sid = f"{name}-{set_code}-{collector_number}"
        if oracle_id is None:
            oracle_id = f"oracle-{name.casefold()}"
        elif oracle_id is False:
            oracle_id = None
        conn.execute(
            "INSERT INTO cards (scryfall_id, name, set_code, collector_number, oracle_id)"
            " VALUES (?,?,?,?,?)",
            (sid, name, set_code, collector_number, oracle_id),
        )
        conn.commit()
        return sid
    return _add
