"""
Data-access layer: plain sqlite3 + pandas, no Streamlit imports. Every
function takes an open sqlite3.Connection as its first argument, so this
module can be unit-tested directly against mtg_collection.db (or the
offline _test_mtg_collection.db) without launching the dashboard.
"""
import sqlite3
import pandas as pd

# Shared column set for anything that joins in a `cards` row. Kept as one
# constant so every card-bearing query (collection, deck_cards, maybeboard)
# returns the same shape and can flow through the same rendering helpers.
CARD_COLUMNS = """
    c.scryfall_id, c.name, c.set_code, c.collector_number, c.type_line,
    c.mana_cost, c.cmc, c.color_identity, c.rarity, c.image_uri,
    c.local_image_path, c.is_basic_land, c.is_game_changer, c.is_reserved,
    c.commander_legal, c.current_price_usd
"""

# Additive, non-destructive schema upgrades for databases built before a
# given column existed. migrate.py is one-way and won't be re-run once
# you've moved to managing things in the dashboard (see its module
# docstring), so this is how the schema evolves from here on instead —
# applied automatically on every connect, never touches existing data,
# and is a no-op once a database is already current.
_SCHEMA_UPGRADES = [
    ("cards", "is_reserved", "ALTER TABLE cards ADD COLUMN is_reserved BOOLEAN DEFAULT 0"),
]


def ensure_schema(conn):
    for table, column, ddl in _SCHEMA_UPGRADES:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(ddl)
            conn.commit()


def get_connection(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


# ------------------------------------------------------------------
# Decks
# ------------------------------------------------------------------
def list_decks(conn, include_retired=True):
    df = pd.read_sql_query(
        """SELECT deck_id, name, commander, partner, representative,
                  color_identity, deck_type, is_active, successor_deck_id
           FROM decks
           ORDER BY is_active DESC, name COLLATE NOCASE""",
        conn,
    )
    if not include_retired:
        df = df[df["is_active"] == 1].reset_index(drop=True)
    return df


def deck_meta(conn, deck_id):
    row = conn.execute("SELECT * FROM decks WHERE deck_id = ?", (deck_id,)).fetchone()
    return dict(row) if row else {}


def deck_stats_row(conn, deck_id):
    row = conn.execute(
        "SELECT games_played, wins, losses, win_rate FROM deck_stats WHERE deck_id = ?",
        (deck_id,),
    ).fetchone()
    if not row:
        return {"games_played": 0, "wins": 0, "losses": 0, "win_rate": None}
    d = dict(row)
    d["games_played"] = d["games_played"] or 0
    d["wins"] = d["wins"] or 0
    d["losses"] = d["losses"] or 0
    return d


def deck_value_row(conn, deck_id):
    row = conn.execute(
        "SELECT total_cards, total_value FROM deck_value WHERE deck_id = ?",
        (deck_id,),
    ).fetchone()
    if not row:
        return {"total_cards": 0, "total_value": 0.0}
    d = dict(row)
    d["total_cards"] = d["total_cards"] or 0
    d["total_value"] = d["total_value"] or 0.0
    return d


def in_deck_sleeved_count(conn, deck_name):
    """SUM(collection.quantity) where Location matches this deck's name
    exactly (physically sleeved cards, per the Location free-text
    convention). Returns None if nothing matched (not necessarily zero —
    could just mean nothing in the collection is tagged with that
    location)."""
    row = conn.execute(
        "SELECT SUM(quantity) FROM collection WHERE location = ?", (deck_name,)
    ).fetchone()
    return row[0]


_RANK_TABLES = {"win_conditions", "strengths", "weaknesses"}


def deck_rank_list(conn, deck_id, kind):
    if kind not in _RANK_TABLES:
        raise ValueError(f"Unknown rank list kind: {kind!r}")
    table = f"deck_{kind}"
    rows = conn.execute(
        f"SELECT rank, label, description FROM {table} WHERE deck_id = ? ORDER BY rank",
        (deck_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def deck_themes_list(conn, deck_id):
    rows = conn.execute(
        "SELECT theme, role FROM deck_themes WHERE deck_id = ? ORDER BY role, theme",
        (deck_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def deck_mana_tag_summary(conn, deck_id):
    rows = conn.execute(
        "SELECT tag, cards FROM deck_mana_tag_summary WHERE deck_id = ? ORDER BY tag",
        (deck_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def deck_game_changers(conn, deck_id):
    df = pd.read_sql_query(
        """SELECT card_name, scryfall_flag, custom_tag
           FROM deck_game_changers WHERE deck_id = ? ORDER BY card_name""",
        conn,
        params=(deck_id,),
    )
    if df.empty:
        return df

    def status(r):
        if r["scryfall_flag"] and r["custom_tag"]:
            return "Confirmed"
        if r["scryfall_flag"] and not r["custom_tag"]:
            return "Scryfall-only (add a category tag)"
        return "Your list only (Scryfall disagrees)"

    df["status"] = df.apply(status, axis=1)
    return df


def deck_reserved_list_cards(conn, deck_id):
    """Mainboard cards in this deck that are on Scryfall's live Reserved
    List, with the quantity actually run."""
    return pd.read_sql_query(
        """SELECT c.name AS card_name, dc.quantity
           FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
           WHERE dc.deck_id = ? AND c.is_reserved = 1
           ORDER BY c.name""",
        conn,
        params=(deck_id,),
    )


def deck_strategy_tag_counts(conn, deck_id):
    return pd.read_sql_query(
        """SELECT t.label AS tag, COUNT(*) AS card_count, ROUND(AVG(c.cmc), 2) AS avg_cmc
           FROM card_tags ct
           JOIN tags t ON t.tag_id = ct.tag_id
           JOIN cards c ON c.scryfall_id = ct.scryfall_id
           WHERE ct.deck_id = ? AND t.tag_type = 'strategy'
           GROUP BY t.label
           ORDER BY card_count DESC""",
        conn,
        params=(deck_id,),
    )


def deck_mana_curve(conn, deck_id):
    """CMC histogram, non-land cards only (standard convention)."""
    return pd.read_sql_query(
        f"""SELECT c.cmc AS cmc, SUM(dc.quantity) AS count
            FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
            WHERE dc.deck_id = ? AND c.type_line NOT LIKE '%Land%'
            GROUP BY c.cmc ORDER BY c.cmc""",
        conn,
        params=(deck_id,),
    )


def deck_cards_dataframe(conn, deck_id):
    """One row per mainboard card, with quantity and any strategy tag(s)
    (comma-joined — schema supports multiple tags per card-in-deck even
    though today's data only populates one)."""
    return pd.read_sql_query(
        f"""SELECT {CARD_COLUMNS}, dc.quantity,
                   GROUP_CONCAT(DISTINCT t.label) AS strategy_tags
            FROM deck_cards dc
            JOIN cards c ON c.scryfall_id = dc.scryfall_id
            LEFT JOIN card_tags ct
                   ON ct.deck_id = dc.deck_id AND ct.scryfall_id = dc.scryfall_id
            LEFT JOIN tags t ON t.tag_id = ct.tag_id AND t.tag_type = 'strategy'
            WHERE dc.deck_id = ?
            GROUP BY c.scryfall_id
            ORDER BY c.name COLLATE NOCASE""",
        conn,
        params=(deck_id,),
    )


def deck_library_dataframe(conn, deck_id):
    """The actual shuffled-library pool used for draw-probability math:
    the mainboard MINUS the commander (and partner, if any) — those sit in
    the command zone and are never part of the shuffled 99(ish)-card deck.
    Matched by exact name against decks.commander / decks.partner."""
    meta = deck_meta(conn, deck_id)
    df = deck_cards_dataframe(conn, deck_id)
    commander_names = {n for n in (meta.get("commander"), meta.get("partner")) if n}
    if commander_names:
        df = df[~df["name"].isin(commander_names)].reset_index(drop=True)
    return df


def maybeboard_dataframe(conn, deck_id):
    return pd.read_sql_query(
        f"""SELECT {CARD_COLUMNS}, mb.review_flag,
                   COALESCE(rc.name, mb.replace_card_name) AS replace_target,
                   mb.notes
            FROM maybeboard mb
            JOIN cards c ON c.scryfall_id = mb.scryfall_id
            LEFT JOIN cards rc ON rc.scryfall_id = mb.replace_scryfall_id
            WHERE mb.deck_id = ?
            ORDER BY c.name COLLATE NOCASE""",
        conn,
        params=(deck_id,),
    )


# ------------------------------------------------------------------
# Collection
# ------------------------------------------------------------------
def collection_dataframe(conn):
    """One row per collection LOT (collection_id) — i.e. the real schema
    grain, preserving per-lot location/price/date rather than silently
    aggregating rows that may live in different boxes or decks."""
    return pd.read_sql_query(
        f"""SELECT col.collection_id, {CARD_COLUMNS},
                   col.quantity, col.foil, col.location, col.date_acquired,
                   col.price_paid, col.source
            FROM collection col
            JOIN cards c ON c.scryfall_id = col.scryfall_id
            ORDER BY c.name COLLATE NOCASE""",
        conn,
    )


def collection_summary(conn):
    row = conn.execute(
        """SELECT COUNT(*) AS lots,
                  COUNT(DISTINCT scryfall_id) AS unique_printings,
                  SUM(quantity) AS tracked_quantity,
                  SUM(quantity * (SELECT current_price_usd FROM cards
                                   WHERE cards.scryfall_id = collection.scryfall_id)) AS tracked_value
           FROM collection"""
    ).fetchone()
    return dict(row) if row else {}


# ------------------------------------------------------------------
# Home-page summary
# ------------------------------------------------------------------
def dashboard_summary(conn):
    def one(q):
        return conn.execute(q).fetchone()[0]

    return {
        "active_decks": one("SELECT COUNT(*) FROM decks WHERE is_active = 1"),
        "retired_decks": one("SELECT COUNT(*) FROM decks WHERE is_active = 0"),
        "unique_printings": one("SELECT COUNT(*) FROM cards"),
        "collection_lots": one("SELECT COUNT(*) FROM collection"),
        "games_logged": one("SELECT COUNT(*) FROM games"),
        "maybeboard_rows": one("SELECT COUNT(*) FROM maybeboard"),
    }
