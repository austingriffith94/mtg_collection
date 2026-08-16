"""
Migrate the Excel-era CSV exports into the SQLite dashboard database.

Run locally (requires network + `requests`):
    pip install pandas requests
    python migrate.py

Reads from ../data/*.csv, writes ../mtg_collection.db, and prints a
resolution report at the end — review it before trusting the data.

Idempotent-ish: re-running deletes and rebuilds mtg_collection.db from
scratch, since incremental re-import isn't worth the complexity here.
"""
import os
import sys
import sqlite3
import datetime
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from scryfall_lookup import ScryfallClient, to_card_row

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

BASIC_LAND_NAMES = {"plains", "island", "swamp", "mountain", "forest", "wastes"}

RETIRED_DECKS = [
    # name, successor_name
    ("Rakdos Showstopper", "Raktres, Lord of Discounts"),
    ("Vorevold, Sac Master", "Vaevictis's Slot Machines"),
]

TAG_COLUMNS_TO_SKIP = {"", "nan", "none"}


def excel_serial_to_iso(value):
    """Convert an Excel date serial (e.g. 44533) to 'YYYY-MM-DD'. Returns None for NaN."""
    if pd.isna(value):
        return None
    try:
        dt = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(value))
        return dt.isoformat()
    except (ValueError, OverflowError):
        return None


def split_label_description(text):
    """'Big Bois: Big Mana Threats...' -> ('Big Bois', 'Big Mana Threats...')"""
    if pd.isna(text) or not str(text).strip():
        return None, None
    text = str(text).strip()
    if ":" in text:
        label, desc = text.split(":", 1)
        return label.strip(), desc.strip()
    return text, None


class Migrator:
    def __init__(self, db_path=DB_PATH, verbose=True):
        self.verbose = verbose
        if os.path.exists(db_path):
            os.remove(db_path)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        with open(SCHEMA_PATH) as f:
            self.conn.executescript(f.read())

        self.client = ScryfallClient(verbose=verbose)
        self.today = datetime.date.today().isoformat()

        # caches to avoid duplicate Scryfall calls / duplicate inserts
        self._card_cache_by_setnum = {}   # (set, number) -> scryfall_id
        self._card_cache_by_name = {}     # lowercased name -> scryfall_id (first-seen printing)
        self._tag_cache = {}              # (tag_type, label) -> tag_id
        self._deck_id_by_name = {}        # deck name -> deck_id

        self.report = {
            "collection_rows": 0,
            "collection_cards_fetched": 0,
            "deck_cards_resolved_from_collection": 0,
            "deck_cards_resolved_by_name_lookup": 0,
            "deck_cards_unresolved": [],
            "maybeboard_unresolved": [],
            "replace_unresolved": [],
            "games_inserted": 0,
            "participants_unmatched_deck": [],
            "game_changer_drift_your_list_only": [],
            "game_changer_drift_scryfall_only": [],
        }

    def log(self, msg):
        if self.verbose:
            print(msg)

    # ------------------------------------------------------------
    def run(self):
        self.log("=== 1/6 Loading decks + themes + win/strength/weakness ===")
        self.load_decks()

        self.log("=== 2/6 Loading collection (Scryfall: exact set+number) ===")
        self.load_collection()

        self.log("=== 3/6 Loading decklists + strategy tags ===")
        self.load_decklists()

        self.log("=== 4/6 Loading maybeboard ===")
        self.load_maybeboard()

        self.log("=== 5/7 Loading game log ===")
        self.load_games()

        self.log("=== 6/7 Loading Game Changer + mana-tag reference lists ===")
        self.load_static_reference_lists()

        self.log("=== 7/7 Checking Game Changer drift (your list vs live Scryfall flag) ===")
        self.check_game_changer_drift()

        self.conn.commit()
        self.print_report()

    # ------------------------------------------------------------
    def get_or_create_deck(self, name, **fields):
        if name in self._deck_id_by_name:
            return self._deck_id_by_name[name]
        cols = ["name"] + list(fields.keys())
        vals = [name] + list(fields.values())
        placeholders = ",".join("?" * len(vals))
        cur = self.conn.execute(
            f"INSERT INTO decks ({','.join(cols)}) VALUES ({placeholders})", vals
        )
        deck_id = cur.lastrowid
        self._deck_id_by_name[name] = deck_id
        return deck_id

    def load_decks(self):
        dm = pd.read_csv(os.path.join(DATA_DIR, "deck_mapping.csv"))

        for _, row in dm.iterrows():
            deck_id = self.get_or_create_deck(
                row["Decks"],
                commander=row.get("Commander"),
                partner=row.get("Partner") if pd.notna(row.get("Partner")) else None,
                representative=row.get("Deck Representative(s)"),
                color_identity=row.get("Color"),
                deck_type=row.get("Deck Type", "Commander"),
                initially_built=excel_serial_to_iso(row.get("Initially Built")),
                description=row.get("Deck Description") if pd.notna(row.get("Deck Description")) else None,
                combos=row.get("Combos") if pd.notna(row.get("Combos")) else None,
                tutors=row.get("Tutors") if pd.notna(row.get("Tutors")) else None,
                bracket=int(row["Bracket"]) if pd.notna(row.get("Bracket")) else None,
                interaction=int(row["Interaction"]) if pd.notna(row.get("Interaction")) else None,
                is_active=1,
            )

            for i in (1, 2, 3):
                for table, col_prefix in [
                    ("deck_win_conditions", "Win Condition"),
                    ("deck_strengths", "Strength"),
                    ("deck_weaknesses", "Weakness"),
                ]:
                    label, desc = split_label_description(row.get(f"{col_prefix} {i}"))
                    if label:
                        self.conn.execute(
                            f"INSERT INTO {table} (deck_id, rank, label, description) VALUES (?,?,?,?)",
                            (deck_id, i, label, desc),
                        )

        # Retired decks — not in deck_mapping.csv, added manually, is_active=0
        for name, successor_name in RETIRED_DECKS:
            deck_id = self.get_or_create_deck(name, is_active=0)
            successor_id = self._deck_id_by_name.get(successor_name)
            if successor_id:
                self.conn.execute(
                    "UPDATE decks SET successor_deck_id = ? WHERE deck_id = ?",
                    (successor_id, deck_id),
                )

        # Themes — optional file, skip gracefully if not present
        themes_path = os.path.join(DATA_DIR, "deck_themes.csv")
        if os.path.exists(themes_path):
            dt = pd.read_csv(themes_path, index_col=0)
            role_map = {"M": "main", "S": "sub"}
            n_inserted = 0
            for theme, row in dt.iterrows():
                for deck_name, val in row.items():
                    if pd.isna(val) or val not in role_map:
                        continue
                    deck_id = self._deck_id_by_name.get(deck_name)
                    if not deck_id:
                        continue
                    self.conn.execute(
                        "INSERT OR IGNORE INTO deck_themes (deck_id, theme, role) VALUES (?,?,?)",
                        (deck_id, theme, role_map[val]),
                    )
                    n_inserted += 1
            self.log(f"  Inserted {n_inserted} theme assignments.")
        else:
            self.log("  deck_themes.csv not found in data/ — skipping themes "
                     "(re-run after adding it, or import separately later).")

        self.conn.commit()
        self.log(f"  {len(self._deck_id_by_name)} decks loaded "
                 f"({len(RETIRED_DECKS)} retired).")

    # ------------------------------------------------------------
    def get_or_create_card_by_setnum(self, set_code, collector_number, name_hint=None):
        key = (str(set_code).strip().lower(), str(collector_number).strip())
        if key in self._card_cache_by_setnum:
            return self._card_cache_by_setnum[key]

        data = self.client.get_by_set_number(*key)
        if data is None:
            return None
        row = to_card_row(data)
        row["price_updated_at"] = self.today
        row["last_fetched_at"] = self.today
        self._insert_card_row(row)
        self._card_cache_by_setnum[key] = row["scryfall_id"]
        self._card_cache_by_name.setdefault(row["name"].strip().lower(), row["scryfall_id"])
        return row["scryfall_id"]

    def get_or_create_card_by_name(self, name):
        key = name.strip().lower()
        if key in self._card_cache_by_name:
            return self._card_cache_by_name[key]

        data = self.client.get_by_name(name)
        if data is None:
            return None
        row = to_card_row(data)
        row["price_updated_at"] = self.today
        row["last_fetched_at"] = self.today
        self._insert_card_row(row)
        self._card_cache_by_name[key] = row["scryfall_id"]
        self._card_cache_by_setnum[(row["set_code"], row["collector_number"])] = row["scryfall_id"]
        return row["scryfall_id"]

    def _insert_card_row(self, row):
        cols = list(row.keys())
        placeholders = ",".join("?" * len(cols))
        self.conn.execute(
            f"INSERT OR IGNORE INTO cards ({','.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )

    # ------------------------------------------------------------
    def load_collection(self):
        c = pd.read_csv(os.path.join(DATA_DIR, "collection.csv"))
        for _, row in c.iterrows():
            self.report["collection_rows"] += 1
            scryfall_id = self.get_or_create_card_by_setnum(
                row["Set Code"], row["Number"], name_hint=row.get("Name")
            )
            if scryfall_id is None:
                self.report["deck_cards_unresolved"].append(
                    f"[collection] {row.get('Name')} ({row['Set Code']}/{row['Number']}) — not found on Scryfall"
                )
                continue
            self.report["collection_cards_fetched"] += 1

            quantity = row["Count"] if pd.notna(row["Count"]) else None
            self.conn.execute(
                """INSERT INTO collection
                   (scryfall_id, quantity, foil, location, date_acquired, price_paid, source)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    scryfall_id,
                    quantity,
                    1 if row["Finish"] == "F" else 0,
                    row.get("Location") if pd.notna(row.get("Location")) else None,
                    excel_serial_to_iso(row.get("Order/Add Date")),
                    float(row["Price Per Card"]) if pd.notna(row.get("Price Per Card")) else None,
                    row.get("Order Note") if pd.notna(row.get("Order Note")) else None,
                ),
            )
        self.conn.commit()
        self.log(f"  {self.report['collection_rows']} collection rows processed, "
                 f"{self.report['collection_cards_fetched']} unique printings fetched.")

    # ------------------------------------------------------------
    def resolve_card_preferring_owned(self, name):
        """Prefer a printing already in the collection (correct owned art);
        fall back to a fresh Scryfall name lookup if not owned."""
        key = name.strip().lower()
        if key in self._card_cache_by_name:
            return self._card_cache_by_name[key], "collection"
        scryfall_id = self.get_or_create_card_by_name(name)
        return scryfall_id, "name_lookup" if scryfall_id else None

    def get_or_create_tag(self, tag_type, label):
        key = (tag_type, label)
        if key in self._tag_cache:
            return self._tag_cache[key]
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO tags (tag_type, label) VALUES (?,?)", key
        )
        if cur.lastrowid:
            tag_id = cur.lastrowid
        else:
            tag_id = self.conn.execute(
                "SELECT tag_id FROM tags WHERE tag_type=? AND label=?", key
            ).fetchone()[0]
        self._tag_cache[key] = tag_id
        return tag_id

    def load_decklists(self):
        d = pd.read_csv(os.path.join(DATA_DIR, "decks.csv"))
        n_rows = 0
        for _, row in d.iterrows():
            deck_id = self._deck_id_by_name.get(row["Deck"])
            if not deck_id:
                self.report["deck_cards_unresolved"].append(
                    f"[decks] Unknown deck '{row['Deck']}' for card '{row['Card']}'"
                )
                continue

            scryfall_id, source = self.resolve_card_preferring_owned(row["Card"])
            if scryfall_id is None:
                self.report["deck_cards_unresolved"].append(
                    f"[decks] '{row['Card']}' in '{row['Deck']}' — could not resolve on Scryfall"
                )
                continue
            if source == "collection":
                self.report["deck_cards_resolved_from_collection"] += 1
            else:
                self.report["deck_cards_resolved_by_name_lookup"] += 1

            self.conn.execute(
                "INSERT OR REPLACE INTO deck_cards (deck_id, scryfall_id, quantity) VALUES (?,?,?)",
                (deck_id, scryfall_id, int(row["Card Count"])),
            )

            tag_label = row.get("Custom Tags")
            if pd.notna(tag_label) and str(tag_label).strip().lower() not in TAG_COLUMNS_TO_SKIP:
                tag_id = self.get_or_create_tag("strategy", str(tag_label).strip())
                self.conn.execute(
                    "INSERT OR IGNORE INTO card_tags (deck_id, scryfall_id, tag_id) VALUES (?,?,?)",
                    (deck_id, scryfall_id, tag_id),
                )
            n_rows += 1
        self.conn.commit()
        self.log(f"  {n_rows} decklist rows loaded "
                 f"({self.report['deck_cards_resolved_from_collection']} from owned collection, "
                 f"{self.report['deck_cards_resolved_by_name_lookup']} via fresh name lookup).")

    # ------------------------------------------------------------
    def load_maybeboard(self):
        mb = pd.read_csv(os.path.join(DATA_DIR, "maybeboard.csv"))

        # Build a per-deck name->scryfall_id index from deck_cards, for resolving "Replace"
        deck_card_names = {}
        for deck_id, name_lower, scryfall_id in self.conn.execute(
            """SELECT dc.deck_id, LOWER(c.name), c.scryfall_id
               FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id"""
        ):
            deck_card_names.setdefault(deck_id, {})[name_lower] = scryfall_id

        n_rows = 0
        for _, row in mb.iterrows():
            deck_id = self._deck_id_by_name.get(row["Deck"])
            if not deck_id:
                self.report["maybeboard_unresolved"].append(
                    f"Unknown deck '{row['Deck']}' for maybeboard card '{row['Card']}'"
                )
                continue

            scryfall_id, source = self.resolve_card_preferring_owned(row["Card"])
            if scryfall_id is None:
                self.report["maybeboard_unresolved"].append(
                    f"'{row['Card']}' in '{row['Deck']}' — could not resolve on Scryfall"
                )
                continue

            replace_id = None
            replace_name_raw = row.get("Replace") if pd.notna(row.get("Replace")) else None
            if replace_name_raw:
                replace_id = deck_card_names.get(deck_id, {}).get(replace_name_raw.strip().lower())
                if replace_id is None:
                    self.report["replace_unresolved"].append(
                        f"'{replace_name_raw}' (replace target for '{row['Card']}' in '{row['Deck']}') "
                        f"not found in that deck's mainboard — kept as free text"
                    )

            self.conn.execute(
                """INSERT OR REPLACE INTO maybeboard
                   (deck_id, scryfall_id, review_flag, replace_scryfall_id, replace_card_name, notes)
                   VALUES (?,?,?,?,?,?)""",
                (
                    deck_id,
                    scryfall_id,
                    1 if row.get("Review") == "x" else 0,
                    replace_id,
                    replace_name_raw,
                    row.get("Notes") if pd.notna(row.get("Notes")) else None,
                ),
            )
            n_rows += 1
        self.conn.commit()
        self.log(f"  {n_rows} maybeboard rows loaded.")

    # ------------------------------------------------------------
    def load_games(self):
        g = pd.read_csv(os.path.join(DATA_DIR, "games_played.csv"))
        n_games = 0
        for _, row in g.iterrows():
            game_id = self.conn.execute(
                "INSERT INTO games (date, note) VALUES (?,?)",
                (
                    excel_serial_to_iso(row["Date"]),
                    row.get("Note") if pd.notna(row.get("Note")) else None,
                ),
            ).lastrowid

            seats = [row["My Deck"], row["Person1"], row["Person2"], row["Person3"]]
            winner = row["Winner"]
            for seat_num, deck_name in enumerate(seats, start=1):
                if pd.isna(deck_name):
                    continue
                deck_name = str(deck_name).strip()
                deck_id = self._deck_id_by_name.get(deck_name)
                if deck_id is None:
                    self.report["participants_unmatched_deck"].append(deck_name)
                self.conn.execute(
                    """INSERT INTO game_participants
                       (game_id, deck_name, deck_id, is_own_deck, is_winner, seat)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        game_id,
                        deck_name,
                        deck_id,
                        1 if deck_id is not None else 0,
                        1 if deck_name == str(winner).strip() else 0,
                        seat_num,
                    ),
                )
            n_games += 1
        self.report["games_inserted"] = n_games
        self.conn.commit()
        self.log(f"  {n_games} games loaded.")

    # ------------------------------------------------------------
    def load_static_reference_lists(self):
        """Load game_changers.csv and mana_tags.csv — pure name+tag
        reference tables, no Scryfall calls needed since they don't
        require printing-level detail."""
        gc_path = os.path.join(DATA_DIR, "game_changers.csv")
        if os.path.exists(gc_path):
            gc = pd.read_csv(gc_path)
            n = 0
            for _, row in gc.iterrows():
                self.conn.execute(
                    "INSERT OR IGNORE INTO game_changer_tags (card_name, tag) VALUES (?,?)",
                    (row["Game Changers"].strip(), row["Tag"].strip()),
                )
                n += 1
            self.log(f"  {n} game_changer_tags rows loaded ({gc['Game Changers'].nunique()} unique cards).")
        else:
            self.log("  game_changers.csv not found — skipping custom Game Changer categories.")

        mt_path = os.path.join(DATA_DIR, "mana_tags.csv")
        if os.path.exists(mt_path):
            mt = pd.read_csv(mt_path)
            n = 0
            for _, row in mt.iterrows():
                self.conn.execute(
                    "INSERT OR IGNORE INTO mana_tags (card_name, tag) VALUES (?,?)",
                    (row["Optimized Mana"].strip(), row["Tag"].strip()),
                )
                n += 1
            self.log(f"  {n} mana_tags rows loaded ({mt['Optimized Mana'].nunique()} unique cards).")
        else:
            self.log("  mana_tags.csv not found — skipping optimized-mana categories.")

        self.conn.commit()

    def check_game_changer_drift(self):
        """Compare your custom game_changer_tags list against Scryfall's
        live `game_changer` field, but only for cards actually present in
        your cards table (owned/decked) — no point flagging drift on cards
        you don't have data for."""
        # In your list, but Scryfall's live flag says no (list may be stale,
        # or Scryfall hasn't caught up to a recent Bracket-list update)
        only_in_your_list = self.conn.execute(
            """SELECT DISTINCT gct.card_name FROM game_changer_tags gct
               JOIN cards c ON c.name = gct.card_name
               WHERE c.is_game_changer = 0"""
        ).fetchall()

        # Scryfall flags it, but it's not in your custom list (missing category tag)
        only_in_scryfall = self.conn.execute(
            """SELECT DISTINCT c.name FROM cards c
               WHERE c.is_game_changer = 1
               AND c.name NOT IN (SELECT card_name FROM game_changer_tags)"""
        ).fetchall()

        self.report["game_changer_drift_your_list_only"] = [r[0] for r in only_in_your_list]
        self.report["game_changer_drift_scryfall_only"] = [r[0] for r in only_in_scryfall]

    # ------------------------------------------------------------
    def print_report(self):
        print("\n" + "=" * 60)
        print("MIGRATION REPORT")
        print("=" * 60)
        print(f"Collection rows processed:            {self.report['collection_rows']}")
        print(f"Unique printings fetched from Scryfall: {self.report['collection_cards_fetched']}")
        print(f"Deck cards resolved via owned printing: {self.report['deck_cards_resolved_from_collection']}")
        print(f"Deck cards resolved via fresh lookup:   {self.report['deck_cards_resolved_by_name_lookup']}")
        print(f"Games loaded:                           {self.report['games_inserted']}")

        if self.report["deck_cards_unresolved"]:
            print(f"\n⚠ {len(self.report['deck_cards_unresolved'])} unresolved deck/collection cards:")
            for item in self.report["deck_cards_unresolved"]:
                print(f"   - {item}")

        if self.report["maybeboard_unresolved"]:
            print(f"\n⚠ {len(self.report['maybeboard_unresolved'])} unresolved maybeboard cards:")
            for item in self.report["maybeboard_unresolved"]:
                print(f"   - {item}")

        if self.report["replace_unresolved"]:
            print(f"\n⚠ {len(self.report['replace_unresolved'])} unresolved 'Replace' targets "
                  f"(kept as free text, no FK link):")
            for item in self.report["replace_unresolved"]:
                print(f"   - {item}")

        if self.report["participants_unmatched_deck"]:
            uniq = sorted(set(self.report["participants_unmatched_deck"]))
            print(f"\nℹ {len(uniq)} opponent/untracked deck names in game log "
                  f"(expected — these are other players' decks, not an error):")
            for item in uniq:
                print(f"   - {item}")

        if self.report["game_changer_drift_your_list_only"]:
            items = self.report["game_changer_drift_your_list_only"]
            print(f"\nℹ {len(items)} card(s) in your Game Changer list, but Scryfall's live "
                  f"flag currently says no (only checked for cards actually in your data):")
            for item in items:
                print(f"   - {item}")

        if self.report["game_changer_drift_scryfall_only"]:
            items = self.report["game_changer_drift_scryfall_only"]
            print(f"\nℹ {len(items)} card(s) Scryfall flags as Game Changer but aren't in your "
                  f"custom category list yet (no sub-category tag — consider adding):")
            for item in items:
                print(f"   - {item}")

        print("\nScryfall client misses:")
        self.client.print_misses()
        print("=" * 60)


if __name__ == "__main__":
    Migrator().run()
