"""
Moxfield plain-text DECK import format:

    1 Combustible Gearhulk (C21) 163
    1 Birgi, God of Storytelling // Harnfel, Horn of Bounty (KHM) 129

Shared by scripts/export_moxfield.py (CLI) and the Decks
page's in-dashboard export section, so both always produce identical
output. No Streamlit dependency — plain sqlite3, unit-testable directly.

Uses the exact printing stored in deck_cards/maybeboard, which migrate.py
(and the Editor page's card_resolver) prefer your OWNED collection
printing for wherever you have a copy — that's what makes the pasted-in
art match what you actually have.

Prompt Pass 6 added the Moxfield COLLECTION CSV import format below
(a completely separate Moxfield feature from the deck paste format
above — one uploads a .csv of everything you own, the other pastes a
single deck's list). Same shared CLI/dashboard-module pattern:
scripts/export_moxfield_collection.py (CLI) and the Collection page's
in-dashboard export section both call the functions below, and still no
Streamlit or pandas dependency — plain sqlite3 + the stdlib `csv` module,
so the CLI script needs nothing beyond Python itself, same as the deck
exporter above.
"""
import csv
import datetime
import io


def format_line(name, set_code, collector_number, quantity=1):
    qty = quantity if quantity else 1
    return f"{qty} {name} ({set_code.upper()}) {collector_number}"


def export_deck_lines(conn, deck_id, include_maybeboard=False):
    """List of text lines in Moxfield's paste format for the deck's
    mainboard, optionally followed by a '// Maybeboard' section."""
    lines = []
    rows = conn.execute(
        """SELECT c.name, c.set_code, c.collector_number, dc.quantity
           FROM deck_cards dc JOIN cards c ON c.scryfall_id = dc.scryfall_id
           WHERE dc.deck_id = ?
           ORDER BY c.name""",
        (deck_id,),
    ).fetchall()
    for name, set_code, collector_number, qty in rows:
        lines.append(format_line(name, set_code, collector_number, qty))

    if include_maybeboard:
        mb_rows = conn.execute(
            """SELECT c.name, c.set_code, c.collector_number
               FROM maybeboard mb JOIN cards c ON c.scryfall_id = mb.scryfall_id
               WHERE mb.deck_id = ?
               ORDER BY c.name""",
            (deck_id,),
        ).fetchall()
        if mb_rows:
            lines.append("")
            lines.append("// Maybeboard")
            for name, set_code, collector_number in mb_rows:
                lines.append(format_line(name, set_code, collector_number, 1))

    return lines


def export_deck_text(conn, deck_id, include_maybeboard=False):
    return "\n".join(export_deck_lines(conn, deck_id, include_maybeboard))


# ------------------------------------------------------------------
# Moxfield COLLECTION CSV import format (Prompt Pass 6). Header order and
# every field's exact meaning/casing were taken directly from Moxfield's
# own sample export (see moxfield_samples/moxfield_collection.csv) and
# the prompt spec — verified byte-for-byte against that sample by
# scripts/_test_prompt_pass6_offline.py.
# ------------------------------------------------------------------
COLLECTION_CSV_HEADERS = [
    "Count", "Tradelist Count", "Name", "Edition", "Condition", "Language",
    "Foil", "Tags", "Last Modified", "Collector Number", "Alter", "Proxy",
    "Purchase Price",
]


def collection_export_rows(conn, only_not_in_deck=False):
    """One dict per printing+foil combination owned, aggregated across
    every collection LOT for that exact printing (scryfall_id) and foil
    status — collection.py intentionally keeps separate lot rows per
    acquisition (different price/date history), but Moxfield's own CSV
    format expects one row per printing/foil combination, not one row
    per lot, so quantity and purchase price are SUMmed across whatever
    lot(s) share a printing+foil here.

    Untracked bulk lots (collection.quantity IS NULL — e.g. an uncounted
    pile of basics) are excluded entirely, matching how the rest of the
    app already treats "tracked" totals (see the Collection page's
    tracked-value caption: those lots are in the collection but never
    summed into a count).

    `only_not_in_deck=True` restricts the result to printings with ZERO
    usage in any deck's mainboard at all — the "Owned NOT in a deck"
    export subset from the prompt spec. Each returned dict has:
    name, set_code, collector_number, foil (bool), quantity (int),
    purchase_price (float or None), in_deck (bool) — the last two power
    collection_csv_row()'s Purchase Price / Tradelist Count columns.

    Uses plain positional column access (not sqlite3.Row / dict-cursor)
    so this works whether `conn` came from dashboard_lib.db (row_factory
    set) or a bare sqlite3.connect() (the CLI script's usage, matching
    export_deck_lines()'s convention above)."""
    raw = conn.execute(
        """SELECT c.name, c.set_code, c.collector_number, col.foil,
                  SUM(col.quantity), SUM(col.price_paid),
                  EXISTS (
                      SELECT 1 FROM deck_cards dc WHERE dc.scryfall_id = c.scryfall_id
                  )
           FROM collection col
           JOIN cards c ON c.scryfall_id = col.scryfall_id
           WHERE col.quantity IS NOT NULL AND col.quantity > 0
           GROUP BY c.scryfall_id, col.foil
           ORDER BY c.name COLLATE NOCASE"""
    ).fetchall()

    rows = []
    for name, set_code, collector_number, foil, qty, paid, in_deck in raw:
        in_deck = bool(in_deck)
        if only_not_in_deck and in_deck:
            continue
        rows.append(
            {
                "name": name,
                "set_code": set_code,
                "collector_number": collector_number,
                "foil": bool(foil),
                "quantity": int(qty or 0),
                "purchase_price": paid,
                "in_deck": in_deck,
            }
        )
    return rows


def collection_csv_row(row, timestamp):
    """One `collection_export_rows()` dict -> one Moxfield CSV row dict,
    keyed by COLLECTION_CSV_HEADERS. `timestamp` is the "Last Modified"
    value to stamp on every row — see export_collection_csv_text()."""
    qty = row["quantity"]
    paid = row["purchase_price"]
    return {
        "Count": qty,
        # "Quantity owned that is NOT currently in a deck (set to 0 if
        # in a deck)" — per spec, applies uniformly whether this row came
        # from the Full Collection or the "Owned NOT in a deck" export
        # (where in_deck is always False anyway, so this is just `qty`).
        "Tradelist Count": 0 if row["in_deck"] else qty,
        "Name": row["name"],
        "Edition": (row["set_code"] or "").lower(),
        "Condition": "Near Mint",
        "Language": "English",
        "Foil": "foil" if row["foil"] else "",
        "Tags": "",
        "Last Modified": timestamp,
        "Collector Number": row["collector_number"],
        "Alter": "FALSE",
        "Proxy": "FALSE",
        "Purchase Price": f"{paid:.2f}" if paid is not None else "",
    }


def export_collection_csv_text(conn, only_not_in_deck=False, now=None):
    """CSV text (header + one row per printing/foil combo) in Moxfield's
    Collection import format, for either the Full Collection or the
    "Owned NOT in a deck" subset. `now` is injectable for deterministic
    tests; defaults to the current time, since collection.py has no real
    per-lot "last modified" column to pull a historical value from (the
    prompt spec's fallback: "or default to current timestamp")."""
    stamp = (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    rows = collection_export_rows(conn, only_not_in_deck=only_not_in_deck)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLLECTION_CSV_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow(collection_csv_row(row, stamp))
    return buf.getvalue()
