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
    c.mana_cost, c.cmc, c.color_identity, c.oracle_text, c.rarity, c.image_uri,
    c.local_image_path, c.is_basic_land, c.is_game_changer, c.is_reserved,
    c.is_showcase, c.is_borderless,
    c.commander_legal, c.current_price_usd
"""
# cards.edhrec_salt is no longer selected here as of Prompt Pass 5 — the
# EDHREC Salt Score feature was retired dashboard-wide (see
# PROJECT_STATE.md): no lightweight public API exposes it, and the only
# alternative data source (MTGJSON's edhrecSaltiness field) only ships
# inside its full AllPrintings-scale exports, which are far too heavy an
# ongoing dependency for one hand-sized field in a personal-scale tool.
# The column itself is left in place on cards (additive schema policy —
# see below), so anyone's previously hand-entered values aren't lost, it
# just isn't read into the dashboard anymore.
# oracle_text (Phase 3) feeds dashboard_lib.formatting's MDFC-aware land
# detection and mana-source color classification (has_land_face(),
# classify_mana_colors(), is_mana_rock_or_dork()) — it isn't rendered as
# its own table column anywhere, only consumed by those pure functions.

# Additive, non-destructive schema upgrades for databases built before a
# given column existed. migrate.py is one-way and won't be re-run once
# you've moved to managing things in the dashboard (see its module
# docstring), so this is how the schema evolves from here on instead —
# applied automatically on every connect, never touches existing data,
# and is a no-op once a database is already current.
_SCHEMA_UPGRADES = [
    ("cards", "is_reserved", "ALTER TABLE cards ADD COLUMN is_reserved BOOLEAN DEFAULT 0"),
    # Phase 2 additions:
    ("cards", "is_showcase", "ALTER TABLE cards ADD COLUMN is_showcase BOOLEAN DEFAULT 0"),
    ("cards", "is_borderless", "ALTER TABLE cards ADD COLUMN is_borderless BOOLEAN DEFAULT 0"),
    ("cards", "edhrec_salt", "ALTER TABLE cards ADD COLUMN edhrec_salt REAL"),
    ("decks", "cover_image_path", "ALTER TABLE decks ADD COLUMN cover_image_path TEXT"),
    ("game_participants", "player_name", "ALTER TABLE game_participants ADD COLUMN player_name TEXT"),
]

# Additive, non-destructive NEW TABLES for databases built before a given
# table existed (same philosophy as _SCHEMA_UPGRADES above, just for
# whole tables instead of columns — CREATE TABLE IF NOT EXISTS is
# naturally a no-op on a database that already has it). Each entry is
# (table_name, ddl, seed_function_or_None); seed_function runs ONLY the
# first time the table is created on a given database, so a table you've
# since edited (added/removed entries) never gets silently re-seeded.
def _seed_theme_catalog(conn):
    conn.execute(
        """INSERT OR IGNORE INTO theme_catalog (theme)
           SELECT DISTINCT theme FROM deck_themes"""
    )


def _seed_game_changer_category_catalog(conn):
    conn.execute(
        """INSERT OR IGNORE INTO game_changer_category_catalog (category)
           SELECT DISTINCT tag FROM game_changer_tags"""
    )


_SCHEMA_TABLE_UPGRADES = [
    ("theme_catalog", "CREATE TABLE theme_catalog (theme TEXT PRIMARY KEY)", _seed_theme_catalog),
    (
        "game_changer_category_catalog",
        "CREATE TABLE game_changer_category_catalog (category TEXT PRIMARY KEY)",
        _seed_game_changer_category_catalog,
    ),
]

# Same additive philosophy as _SCHEMA_TABLE_UPGRADES, but for VIEWS (kept
# separate since sqlite_master distinguishes type='table' from
# type='view', and a view has no rows to seed).
_SCHEMA_VIEW_UPGRADES = [
    (
        "player_stats",
        """CREATE VIEW player_stats AS
           SELECT
               gp.player_name AS player_name,
               COUNT(*) AS games_played,
               SUM(CASE WHEN gp.is_winner THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN NOT gp.is_winner THEN 1 ELSE 0 END) AS losses,
               ROUND(1.0 * SUM(CASE WHEN gp.is_winner THEN 1 ELSE 0 END)
                     / NULLIF(COUNT(*), 0), 3) AS win_rate
           FROM game_participants gp
           WHERE gp.player_name IS NOT NULL AND TRIM(gp.player_name) != ''
           GROUP BY gp.player_name""",
    ),
]


def _normalize_retired_decks(conn):
    """Phase 1 removed the Retired Decks feature — the app now treats
    every tracked deck as active, full stop. Databases built before this
    change may still physically carry the old `is_active`/
    `successor_deck_id` columns (dropping columns from a live SQLite file
    is intentionally avoided here, in keeping with this project's
    additive/non-destructive schema-upgrade policy — see _SCHEMA_UPGRADES
    above). Rather than leave stale "retired" data lying around for code
    that no longer looks at it, this normalizes those columns in place —
    every deck becomes active and any successor link is cleared — which
    is idempotent and a no-op after the first run on a given database."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(decks)").fetchall()}
    changed = False
    if "is_active" in cols:
        conn.execute("UPDATE decks SET is_active = 1 WHERE is_active != 1")
        changed = True
    if "successor_deck_id" in cols:
        conn.execute("UPDATE decks SET successor_deck_id = NULL WHERE successor_deck_id IS NOT NULL")
        changed = True
    if changed:
        conn.commit()


def _existing_objects(conn, kind):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (kind,)
        ).fetchall()
    }


def ensure_schema(conn):
    for table, column, ddl in _SCHEMA_UPGRADES:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(ddl)
            conn.commit()

    existing_tables = _existing_objects(conn, "table")
    for table, ddl, seed_fn in _SCHEMA_TABLE_UPGRADES:
        if table not in existing_tables:
            conn.execute(ddl)
            conn.commit()
            if seed_fn:
                seed_fn(conn)
                conn.commit()

    existing_views = _existing_objects(conn, "view")
    for view, ddl in _SCHEMA_VIEW_UPGRADES:
        if view not in existing_views:
            conn.execute(ddl)
            conn.commit()

    _normalize_retired_decks(conn)


def get_connection(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


# ------------------------------------------------------------------
# Decks
# ------------------------------------------------------------------
def list_decks(conn):
    return pd.read_sql_query(
        """SELECT deck_id, name, commander, partner, representative,
                  color_identity, deck_type
           FROM decks
           ORDER BY name COLLATE NOCASE""",
        conn,
    )


def deck_meta(conn, deck_id):
    row = conn.execute("SELECT * FROM decks WHERE deck_id = ?", (deck_id,)).fetchone()
    return dict(row) if row else {}


def list_decks_with_covers(conn):
    """Same deck list as list_decks(), plus everything the home-page
    landing grid (Prompt Pass 4) needs to pick a thumbnail per deck: the
    user-set cover_image_path if any, and — as a fallback for decks
    without one — the commander's own card art, resolved by matching
    decks.commander against the deck's own deck_cards/cards rows (not a
    global name lookup) so a commander owned across multiple printings
    still resolves to the specific printing actually run in that deck.
    MAX() is used purely to collapse the one-to-many deck_cards join
    down to a single row per deck; every non-aggregated deck_cards row
    besides the commander's own naturally has NULL for the two
    commander_* columns being maxed, so this doesn't average or pick
    among several candidates."""
    return pd.read_sql_query(
        """SELECT d.deck_id, d.name, d.representative, d.commander,
                  d.cover_image_path,
                  MAX(c.image_uri) AS commander_image_uri,
                  MAX(c.local_image_path) AS commander_local_image_path
           FROM decks d
           LEFT JOIN deck_cards dc ON dc.deck_id = d.deck_id
           LEFT JOIN cards c ON c.scryfall_id = dc.scryfall_id AND c.name = d.commander
           GROUP BY d.deck_id
           ORDER BY d.name COLLATE NOCASE""",
        conn,
    )


def cards_by_name(conn, names):
    """Look up cards.* rows (mana_cost/cmc/color_identity/type_line/
    oracle_text) by exact, case-insensitive name — used by the Commander
    Cast Probability engine (Phase 3) to find the commander/partner's own
    card data, since the commander itself is excluded from
    deck_library_dataframe (it lives in the command zone, never in the
    shuffled library). Grouped by name so a commander owned across
    multiple printings still returns one row."""
    names = [n for n in (names or []) if n]
    if not names:
        return pd.DataFrame(columns=["name", "mana_cost", "cmc", "color_identity", "type_line", "oracle_text"])
    placeholders = ",".join("?" for _ in names)
    return pd.read_sql_query(
        f"""SELECT name, mana_cost, cmc, color_identity, type_line, oracle_text
            FROM cards WHERE name COLLATE NOCASE IN ({placeholders})
            GROUP BY name""",
        conn,
        params=names,
    )


# ------------------------------------------------------------------
# Card name autocomplete + printing lookup (Prompt Pass 6) — powers the
# Editor's Collection tab "add a card" flow: search_card_names() drives
# the type-ahead suggestion list, card_printings_by_name() drives the
# second "choose a printing" selector once a name is picked. Both query
# the local `cards` table only (the dashboard's own "master card
# registry" — no network needed); card_resolver.fetch_additional_printings()
# is the explicit, user-initiated way to extend that registry with
# printings Scryfall knows about but this database doesn't yet.
# ------------------------------------------------------------------
def search_card_names(conn, query_text, limit=25):
    """Distinct card names from `cards` matching `query_text` as a
    case-insensitive substring, names that START WITH the query ranked
    ahead of names that merely contain it, alphabetical within each
    group. Returns [] for blank input rather than the whole table."""
    query_text = (query_text or "").strip()
    if not query_text:
        return []
    contains_pattern = f"%{query_text}%"
    starts_pattern = f"{query_text}%"
    rows = conn.execute(
        """SELECT DISTINCT name FROM cards
           WHERE name LIKE ? COLLATE NOCASE
           ORDER BY CASE WHEN name LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,
                    name COLLATE NOCASE
           LIMIT ?""",
        (contains_pattern, starts_pattern, limit),
    ).fetchall()
    return [r[0] for r in rows]


_PRINTING_COLUMNS = [
    "scryfall_id", "name", "set_code", "collector_number", "rarity",
    "current_price_usd", "image_uri", "local_image_path",
]


def card_printings_by_name(conn, name):
    """Every printing of `name` already known locally (one dict per
    `cards` row: scryfall_id/set_code/collector_number/rarity/
    current_price_usd/image_uri/local_image_path), exact case-insensitive
    name match, ordered by set then collector number. Returns [] if the
    name isn't in the local database under any printing yet. Builds dicts
    from plain tuples (not sqlite3.Row) so this works regardless of the
    caller's row_factory setting."""
    name = (name or "").strip()
    if not name:
        return []
    rows = conn.execute(
        f"""SELECT {", ".join(_PRINTING_COLUMNS)}
           FROM cards WHERE name = ? COLLATE NOCASE
           ORDER BY set_code COLLATE NOCASE, collector_number COLLATE NOCASE""",
        (name,),
    ).fetchall()
    return [dict(zip(_PRINTING_COLUMNS, r)) for r in rows]


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


def deck_price_top10(conn, deck_id):
    """Top 10 most expensive mainboard cards (by current_price_usd,
    single-copy unit pricing — quantity is intentionally not shown/used
    here), priced cards only, ties broken alphabetically. Also surfaces
    what was actually paid for this card (collection.price_paid, summed
    across any lot(s) whose location matches this deck's name, i.e. the
    copy(ies) actually sleeved in it) as `purchase_price` — NULL when
    nothing's been logged as physically sleeved here."""
    return pd.read_sql_query(
        """SELECT c.name AS card_name, c.current_price_usd AS price,
                  (SELECT SUM(col.price_paid) FROM collection col
                    WHERE col.scryfall_id = c.scryfall_id AND col.location = d.name
                  ) AS purchase_price
           FROM deck_cards dc
           JOIN cards c ON c.scryfall_id = dc.scryfall_id
           JOIN decks d ON d.deck_id = dc.deck_id
           WHERE dc.deck_id = ? AND c.current_price_usd IS NOT NULL
           ORDER BY c.current_price_usd DESC, c.name COLLATE NOCASE
           LIMIT 10""",
        conn,
        params=(deck_id,),
    )


# deck_salt_top10() (Top 10 Saltiest cards) was removed in Prompt Pass 5
# along with the rest of the EDHREC Salt Score feature — see the
# CARD_COLUMNS comment above and PROJECT_STATE.md for why.


def deck_reserved_list_cards(conn, deck_id):
    """Mainboard cards in this deck that are on Scryfall's live Reserved
    List, with the card's current price (rather than quantity run) —
    what a Reserved List card is worth is usually more actionable at a
    glance than how many copies are in a singleton Commander deck."""
    return pd.read_sql_query(
        """SELECT c.name AS card_name, c.current_price_usd AS price
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
    aggregating rows that may live in different boxes or decks.

    `in_deck` (Prompt Pass 4) is 1 if this printing's scryfall_id is used
    in ANY deck's mainboard (deck_cards), 0 otherwise — the same
    "spoken for by a deck" test writes.prune_collection() already uses,
    reused here to power the Collection page's deck-status filter."""
    return pd.read_sql_query(
        f"""SELECT col.collection_id, {CARD_COLUMNS},
                   col.quantity, col.foil, col.location, col.date_acquired,
                   col.price_paid, col.source,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM deck_cards dc WHERE dc.scryfall_id = col.scryfall_id
                   ) THEN 1 ELSE 0 END AS in_deck
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
# Game Changers overview (Editor -> Game Changers tab) — every card that's
# EITHER Scryfall-flagged (c.is_game_changer) or carries a custom category
# tag (game_changer_tags), across the whole database (not deck-scoped),
# with art plus owned/assigned counters. Matches by card NAME (not
# scryfall_id) for "owned"/"assigned" since the same Game Changer can be
# owned across multiple printings and game_changer_tags is itself
# name-keyed.
# ------------------------------------------------------------------
def game_changers_overview(conn):
    df = pd.read_sql_query(
        """SELECT c.scryfall_id, c.name, c.image_uri, c.local_image_path,
                  c.is_game_changer AS scryfall_flag,
                  GROUP_CONCAT(DISTINCT gct.tag) AS categories
           FROM cards c
           LEFT JOIN game_changer_tags gct ON gct.card_name = c.name
           WHERE c.is_game_changer = 1 OR c.name IN (SELECT DISTINCT card_name FROM game_changer_tags)
           GROUP BY c.name
           ORDER BY c.name COLLATE NOCASE""",
        conn,
    )
    if df.empty:
        return df

    owned = pd.read_sql_query(
        """SELECT c.name AS name, SUM(col.quantity) AS owned_qty
           FROM collection col JOIN cards c ON c.scryfall_id = col.scryfall_id
           GROUP BY c.name""",
        conn,
    )
    assigned = pd.read_sql_query(
        """SELECT c.name AS name, COUNT(DISTINCT dc.deck_id) AS deck_count
           FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
           GROUP BY c.name""",
        conn,
    )
    df = df.merge(owned, on="name", how="left").merge(assigned, on="name", how="left")
    df["owned_qty"] = df["owned_qty"].fillna(0).astype(int)
    df["deck_count"] = df["deck_count"].fillna(0).astype(int)
    return df


# ------------------------------------------------------------------
# Commander Game Tracking (Phase 2)
# ------------------------------------------------------------------
def games_list(conn):
    """One row per logged game with its participants folded into a
    single display-friendly string, newest first, for the Game History
    view (an actual rendered list, not a raw dump of games/
    game_participants)."""
    games = pd.read_sql_query(
        "SELECT game_id, date, note FROM games ORDER BY date DESC, game_id DESC", conn
    )
    if games.empty:
        return games

    parts = pd.read_sql_query(
        """SELECT game_id, seat, deck_name, is_winner, player_name
           FROM game_participants ORDER BY game_id, seat""",
        conn,
    )
    by_game = {gid: grp for gid, grp in parts.groupby("game_id")}

    def render(gid):
        rows = by_game.get(gid)
        if rows is None:
            return []
        out = []
        for _, r in rows.iterrows():
            label = r["deck_name"]
            if pd.notna(r.get("player_name")) and r["player_name"]:
                label += f" ({r['player_name']})"
            if r["is_winner"]:
                label += " 🏆"
            out.append(label)
        return out

    games["participants"] = games["game_id"].apply(render)
    return games


def deck_win_rates(conn):
    """All decks' win/loss/win-rate (deck_stats view), decks with at
    least one logged game only, most-played first."""
    return pd.read_sql_query(
        """SELECT name, games_played, wins, losses, win_rate
           FROM deck_stats WHERE games_played > 0
           ORDER BY games_played DESC, win_rate DESC""",
        conn,
    )


def player_win_rates(conn):
    """Win/loss/win-rate by player_name (player_stats view) — only
    reflects games logged since Phase 2 added player_name; older rows
    won't contribute here."""
    return pd.read_sql_query(
        """SELECT player_name, games_played, wins, losses, win_rate
           FROM player_stats
           ORDER BY games_played DESC, win_rate DESC""",
        conn,
    )


# ------------------------------------------------------------------
# Home-page summary
# ------------------------------------------------------------------
def dashboard_summary(conn):
    def one(q):
        return conn.execute(q).fetchone()[0]

    return {
        "total_decks": one("SELECT COUNT(*) FROM decks"),
        "unique_printings": one("SELECT COUNT(*) FROM cards"),
        "collection_lots": one("SELECT COUNT(*) FROM collection"),
        "games_logged": one("SELECT COUNT(*) FROM games"),
        "maybeboard_rows": one("SELECT COUNT(*) FROM maybeboard"),
    }
