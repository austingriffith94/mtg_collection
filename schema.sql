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
    is_reserved       BOOLEAN DEFAULT 0,  -- Scryfall's `reserved` field (Reserved List)
    is_showcase       BOOLEAN DEFAULT 0,  -- Scryfall frame_effects contains "showcase"
    is_borderless     BOOLEAN DEFAULT 0,  -- Scryfall border_color == "borderless"
    commander_legal   BOOLEAN DEFAULT 1,
    current_price_usd REAL,
    price_updated_at  DATE,
    last_fetched_at   DATE,
    edhrec_salt       REAL              -- EDHREC salt score. Feature retired in Prompt
                                         -- Pass 5 (no clean, low-overhead data source was
                                         -- found — see PROJECT_STATE.md); the column is
                                         -- kept non-destructively per schema policy so any
                                         -- values entered before that pass aren't lost, but
                                         -- the app no longer reads or writes it
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

-- Master list of "standard" (non-deck) storage locations offered in the
-- Editor's Location dropdown (Prompt Pass 13), alongside every currently
-- tracked deck name (queries.location_options() unions the two — a
-- deck's own name is always a valid Location, meaning "sleeved in that
-- deck", without needing its own catalog entry). Seeded with "Box" (also
-- the fallback location a deck's Location rows are reassigned to when
-- that deck is deleted or a Swap Manager swap removes a card from it —
-- see writes.DEFAULT_LOCATION) and "Lands Box". Adding/removing entries
-- here only changes what the dropdown offers; it never touches any
-- collection row's existing Location value — same pattern as
-- theme_catalog/game_changer_category_catalog/mana_tag_catalog above.
CREATE TABLE location_catalog (
    location  TEXT PRIMARY KEY
);

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
    cover_image_path  TEXT              -- relative path under image_cache/deck_covers/,
                                         -- a user-picked PNG shown as the deck's thumbnail
                                         -- (Editor -> Deck Info); NULL if none set
    -- Note: there is no "retired"/active distinction — every tracked deck
    -- is assumed active. (Phase 1 removed the earlier is_active/
    -- successor_deck_id columns; see delete_deck() in writes.py for how a
    -- deck that's genuinely gone is removed instead.)
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

-- Master list of theme names offered in the Editor's Themes dropdown
-- (Phase 2). Seeded once from whatever distinct theme names already
-- exist in deck_themes (originally sourced from deck_themes.csv at
-- migration) — see migrate.py's load_decks() and queries.ensure_schema()'s
-- bootstrap for upgraded pre-Phase-2 databases. Adding/removing entries
-- here only affects what the dropdown offers; it never touches existing
-- deck_themes assignments.
CREATE TABLE theme_catalog (
    theme  TEXT PRIMARY KEY
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
-- Supports many tags per card-in-deck. There is deliberately no
-- deck-level tags table — deck-level notes live in decks.description/
-- combos/tutors instead (see the Editor page's "Card Tags" tab, moved
-- here from the earlier standalone Tag Editor page).
-- ------------------------------------------------------------
CREATE TABLE tags (
    tag_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_type  TEXT NOT NULL,     -- 'strategy', 'rule0', or any custom category
    label     TEXT NOT NULL,
    UNIQUE(tag_type, label)
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

-- Master list of Game Changer categories offered in the Editor's Game
-- Changers dropdown (Phase 2). Seeded once from whatever distinct tag
-- values already exist in game_changer_tags (originally sourced from
-- game_changers.csv at migration). Adding/removing entries here only
-- affects what the dropdown offers; it never touches existing
-- game_changer_tags assignments.
CREATE TABLE game_changer_category_catalog (
    category  TEXT PRIMARY KEY
);

-- Optimized-mana categorization (Fast, Dual, Shockland, Fetch, Ritual,
-- Mana Doubler, Medallion, Moxen, etc.) — reconstructs the "Optimized
-- Mana" footnote from your old PDFs, computed dynamically per deck.
CREATE TABLE mana_tags (
    card_name  TEXT NOT NULL,
    tag        TEXT NOT NULL,
    PRIMARY KEY (card_name, tag)
);

-- Master list of Optimized-mana categories offered in the Editor's Mana
-- Tags dropdown (Prompt Pass 12). Seeded once from whatever distinct tag
-- values already exist in mana_tags (originally sourced from
-- mana_tags.csv at migration). Adding/removing entries here only affects
-- what the dropdown offers; it never touches existing mana_tags
-- assignments — same pattern as game_changer_category_catalog above.
CREATE TABLE mana_tag_catalog (
    tag  TEXT PRIMARY KEY
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
    player_name  TEXT,                                 -- who piloted this deck this game
                                                         -- (Phase 2, Commander Game Tracking tab;
                                                         -- NULL for games logged before this existed)
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

-- Win/loss/win-rate by PLAYER rather than by deck (Phase 2, Commander
-- Game Tracking tab) — only counts participant rows that actually have a
-- player_name recorded, since that field didn't exist before Phase 2 and
-- older game log rows won't have one.
CREATE VIEW player_stats AS
SELECT
    gp.player_name                                      AS player_name,
    COUNT(*)                                             AS games_played,
    SUM(CASE WHEN gp.is_winner THEN 1 ELSE 0 END)        AS wins,
    SUM(CASE WHEN NOT gp.is_winner THEN 1 ELSE 0 END)    AS losses,
    ROUND(
        1.0 * SUM(CASE WHEN gp.is_winner THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0), 3
    )                                                    AS win_rate
FROM game_participants gp
WHERE gp.player_name IS NOT NULL AND TRIM(gp.player_name) != ''
GROUP BY gp.player_name;

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
