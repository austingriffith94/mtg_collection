-- ============================================================
-- MTG Collection & Deck Dashboard — SQLite Schema
-- ============================================================
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- Card reference table (lazily populated from Scryfall).
-- One row per SPECIFIC PRINTING (scryfall id), not per oracle card,
-- since we need exact set/collector number for Moxfield export
-- and for showing the correct card art.
-- ------------------------------------------------------------
CREATE TABLE cards (
    scryfall_id       TEXT PRIMARY KEY,
    oracle_id         TEXT,               -- groups same card across printings
    name              TEXT NOT NULL,      -- includes " // " for MDFCs
    set_code          TEXT NOT NULL,
    collector_number  TEXT NOT NULL,
    type_line         TEXT,
    mana_cost         TEXT,
    cmc               REAL,
    color_identity    TEXT,               -- comma-sep, e.g. "B,R"
    oracle_text       TEXT,
    rarity            TEXT,
    image_uri         TEXT,               -- remote Scryfall URL (fallback)
    local_image_path  TEXT,               -- relative path under image_cache/, once downloaded
    is_basic_land     BOOLEAN DEFAULT 0,
    is_game_changer   BOOLEAN DEFAULT 0,
    commander_legal   BOOLEAN DEFAULT 1,
    current_price_usd REAL,
    price_updated_at  DATE,
    last_fetched_at   DATE
);
CREATE INDEX idx_cards_name ON cards(name);
CREATE INDEX idx_cards_oracle ON cards(oracle_id);

-- ------------------------------------------------------------
-- Physical collection. One row per acquisition lot (same
-- printing/finish bought at different times/prices stays separate
-- rows so price-paid history is preserved).
-- ------------------------------------------------------------
CREATE TABLE collection (
    collection_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    scryfall_id        TEXT NOT NULL REFERENCES cards(scryfall_id),
    quantity           INTEGER,           -- NULL = untracked/bulk quantity (e.g. basic lands)
    foil               BOOLEAN NOT NULL DEFAULT 0,
    location           TEXT,              -- free text: box code OR deck name if sleeved in
    date_acquired      DATE,
    price_paid         REAL,
    source             TEXT               -- acquisition batch, e.g. "Mana Pool", "CK"
);
CREATE INDEX idx_collection_card ON collection(scryfall_id);
CREATE INDEX idx_collection_location ON collection(location);

-- ------------------------------------------------------------
-- Decks
-- ------------------------------------------------------------
CREATE TABLE decks (
    deck_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE,
    commander         TEXT,
    partner           TEXT,
    representative    TEXT,               -- display name (commander + partner combined)
    color_identity    TEXT,
    deck_type         TEXT DEFAULT 'Commander',
    initially_built   DATE,
    description       TEXT,
    combos            TEXT,
    tutors            TEXT,
    bracket           INTEGER,
    interaction       INTEGER,
    is_active         BOOLEAN NOT NULL DEFAULT 1,   -- 0 = retired (kept for game history)
    successor_deck_id INTEGER REFERENCES decks(deck_id)  -- optional link, NOT auto-merged into stats
);

-- Ranked (1-3) win conditions / strengths / weaknesses, each "Label: Description"
CREATE TABLE deck_win_conditions (
    deck_id     INTEGER NOT NULL REFERENCES decks(deck_id),
    rank        INTEGER NOT NULL CHECK(rank IN (1,2,3)),
    label       TEXT,
    description TEXT,
    PRIMARY KEY (deck_id, rank)
);

CREATE TABLE deck_strengths (
    deck_id     INTEGER NOT NULL REFERENCES decks(deck_id),
    rank        INTEGER NOT NULL CHECK(rank IN (1,2,3)),
    label       TEXT,
    description TEXT,
    PRIMARY KEY (deck_id, rank)
);

CREATE TABLE deck_weaknesses (
    deck_id     INTEGER NOT NULL REFERENCES decks(deck_id),
    rank        INTEGER NOT NULL CHECK(rank IN (1,2,3)),
    label       TEXT,
    description TEXT,
    PRIMARY KEY (deck_id, rank)
);

-- Theme matrix: main (M) / sub (S) role per theme per deck
CREATE TABLE deck_themes (
    deck_id  INTEGER NOT NULL REFERENCES decks(deck_id),
    theme    TEXT NOT NULL,
    role     TEXT NOT NULL CHECK(role IN ('main','sub')),
    PRIMARY KEY (deck_id, theme, role)
);

-- ------------------------------------------------------------
-- Decklist (mainboard). Quantity supports basics (e.g. 10 Mountain).
-- ------------------------------------------------------------
CREATE TABLE deck_cards (
    deck_id      INTEGER NOT NULL REFERENCES decks(deck_id),
    scryfall_id  TEXT NOT NULL REFERENCES cards(scryfall_id),
    quantity     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (deck_id, scryfall_id)
);

-- ------------------------------------------------------------
-- Maybeboard (its own table — distinct workflow from mainboard:
-- review flag, a suggested main-deck card to replace, and notes)
-- ------------------------------------------------------------
CREATE TABLE maybeboard (
    deck_id          INTEGER NOT NULL REFERENCES decks(deck_id),
    scryfall_id      TEXT NOT NULL REFERENCES cards(scryfall_id),
    review_flag      BOOLEAN DEFAULT 0,
    replace_scryfall_id TEXT REFERENCES cards(scryfall_id),  -- resolved match, nullable
    replace_card_name   TEXT,   -- raw text fallback if resolution failed
    notes            TEXT,
    PRIMARY KEY (deck_id, scryfall_id)
);

-- ------------------------------------------------------------
-- Flexible multi-valued tagging (strategy tags, Rule 0 tags, etc.)
-- Supports many tags per card-in-deck and many tags per deck.
-- ------------------------------------------------------------
CREATE TABLE tags (
    tag_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_type  TEXT NOT NULL,     -- 'strategy', 'rule0', or any custom category
    label     TEXT NOT NULL,
    UNIQUE(tag_type, label)
);

CREATE TABLE deck_tags (
    deck_id  INTEGER NOT NULL REFERENCES decks(deck_id),
    tag_id   INTEGER NOT NULL REFERENCES tags(tag_id),
    notes    TEXT,
    PRIMARY KEY (deck_id, tag_id)
);

-- Card tags are scoped to a specific deck (same card can carry
-- different strategy tags in different decks)
CREATE TABLE card_tags (
    deck_id      INTEGER NOT NULL REFERENCES decks(deck_id),
    scryfall_id  TEXT NOT NULL REFERENCES cards(scryfall_id),
    tag_id       INTEGER NOT NULL REFERENCES tags(tag_id),
    PRIMARY KEY (deck_id, scryfall_id, tag_id)
);

-- ------------------------------------------------------------
-- Static reference lists (name-keyed, printing-agnostic — these
-- describe the card itself, not a specific owned printing).
-- Loaded directly from your curated CSVs, no Scryfall calls needed.
-- ------------------------------------------------------------

-- Your custom Game Changer categorization (Combo/Mana/Power/Stax-Unfun/
-- Tutor/Value). Separate from cards.is_game_changer, which is pulled
-- live from Scryfall's own official `game_changer` field — this table
-- adds the sub-category Scryfall's flat boolean doesn't provide, and
-- lets us detect drift between your list and Scryfall's (see
-- deck_game_changers view below).
CREATE TABLE game_changer_tags (
    card_name  TEXT NOT NULL,
    tag        TEXT NOT NULL,   -- Combo, Mana, Power, Stax/Unfun, Tutor, Value
    PRIMARY KEY (card_name, tag)
);

-- Optimized-mana categorization (Fast, Dual, Shockland, Fetch, Ritual,
-- Mana Doubler, Medallion, Moxen, etc.) — reconstructs the "Optimized
-- Mana" footnote from your old PDFs, computed dynamically per deck.
CREATE TABLE mana_tags (
    card_name  TEXT NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY (card_name, tag)
);

-- ------------------------------------------------------------
-- Game log. Any deck name is allowed (opponents' decks won't
-- always be in `decks`), so deck_id is a nullable, best-effort link.
-- ------------------------------------------------------------
CREATE TABLE games (
    game_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    date     DATE,
    note     TEXT
);

CREATE TABLE game_participants (
    game_id      INTEGER NOT NULL REFERENCES games(game_id),
    deck_name    TEXT NOT NULL,
    deck_id      INTEGER REFERENCES decks(deck_id),   -- NULL if not a tracked deck
    is_own_deck  BOOLEAN NOT NULL DEFAULT 0,
    is_winner    BOOLEAN NOT NULL DEFAULT 0,
    seat         INTEGER,                              -- 1-4, preserves original column order
    PRIMARY KEY (game_id, seat)
);
CREATE INDEX idx_gp_deck_id ON game_participants(deck_id);
CREATE INDEX idx_gp_deck_name ON game_participants(deck_name);

-- ------------------------------------------------------------
-- Convenience views — win/loss/win-rate computed dynamically,
-- never stored, so they can't go stale.
-- ------------------------------------------------------------
CREATE VIEW deck_stats AS
SELECT
    d.deck_id,
    d.name,
    d.is_active,
    COUNT(gp.game_id)                                   AS games_played,
    SUM(CASE WHEN gp.is_winner THEN 1 ELSE 0 END)        AS wins,
    SUM(CASE WHEN NOT gp.is_winner THEN 1 ELSE 0 END)    AS losses,
    ROUND(
        1.0 * SUM(CASE WHEN gp.is_winner THEN 1 ELSE 0 END)
        / NULLIF(COUNT(gp.game_id), 0), 3
    )                                                    AS win_rate
FROM decks d
LEFT JOIN game_participants gp ON gp.deck_id = d.deck_id
GROUP BY d.deck_id;

CREATE VIEW deck_value AS
SELECT
    dc.deck_id,
    SUM(dc.quantity)                                     AS total_cards,
    SUM(dc.quantity * COALESCE(c.current_price_usd, 0))  AS total_value
FROM deck_cards dc
JOIN cards c ON c.scryfall_id = dc.scryfall_id
GROUP BY dc.deck_id;

-- Reconstructs the old "Optimized Mana" PDF footnote per deck, live —
-- which mana-tag categories are present and which cards carry them.
CREATE VIEW deck_mana_tag_summary AS
SELECT
    dc.deck_id,
    mt.tag,
    GROUP_CONCAT(DISTINCT c.name) AS cards
FROM deck_cards dc
JOIN cards c ON c.scryfall_id = dc.scryfall_id
JOIN mana_tags mt ON mt.card_name = c.name
GROUP BY dc.deck_id, mt.tag;

-- Game Changers actually present in each deck, showing BOTH the live
-- Scryfall flag and your custom category tag side by side. A row with
-- scryfall_flag=1 and custom_tag=NULL means Scryfall has flagged a card
-- your list hasn't caught up to yet (or vice versa) — worth reviewing.
CREATE VIEW deck_game_changers AS
SELECT
    dc.deck_id,
    c.name                AS card_name,
    c.is_game_changer      AS scryfall_flag,
    gct.tag                AS custom_tag
FROM deck_cards dc
JOIN cards c ON c.scryfall_id = dc.scryfall_id
LEFT JOIN game_changer_tags gct ON gct.card_name = c.name
WHERE c.is_game_changer = 1 OR gct.card_name IS NOT NULL
GROUP BY dc.deck_id, c.name;
