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
    """add_card('Sol Ring') -> scryfall_id of a minimal card row."""
    def _add(name, set_code="cmd", collector_number="1"):
        sid = f"{name}-{set_code}-{collector_number}"
        conn.execute(
            "INSERT INTO cards (scryfall_id, name, set_code, collector_number) VALUES (?,?,?,?)",
            (sid, name, set_code, collector_number),
        )
        conn.commit()
        return sid
    return _add
