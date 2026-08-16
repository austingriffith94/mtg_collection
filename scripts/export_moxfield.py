"""
Export a deck to Moxfield's plain-text import format:

    1 Combustible Gearhulk (C21) 163
    1 Birgi, God of Storytelling // Harnfel, Horn of Bounty (KHM) 129

Uses the exact printing stored in deck_cards (which, per migrate.py,
prefers your OWNED collection printing wherever you have a copy —
falling back to a default Scryfall printing only for cards you don't
own yet). That's what makes the pasted-in art match what you actually
have.

Usage:
    python export_moxfield.py "Raktres, Lord of Discounts"
    python export_moxfield.py "Raktres, Lord of Discounts" --maybeboard
    python export_moxfield.py --list          # show all deck names
"""
import os
import sys
import sqlite3
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")


def list_decks(conn):
    print("Decks in database:")
    for name, active in conn.execute(
        "SELECT name, is_active FROM decks ORDER BY is_active DESC, name"
    ):
        tag = "" if active else "  [retired]"
        print(f"  - {name}{tag}")


def format_line(name, set_code, collector_number, quantity):
    qty = quantity if quantity else 1
    return f"{qty} {name} ({set_code.upper()}) {collector_number}"


def export_deck(conn, deck_name, include_maybeboard=False):
    deck_row = conn.execute(
        "SELECT deck_id FROM decks WHERE name = ?", (deck_name,)
    ).fetchone()
    if not deck_row:
        print(f"No deck found named '{deck_name}'. Use --list to see valid names.")
        sys.exit(1)
    deck_id = deck_row[0]

    lines = []
    unresolved = []

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

    return lines, unresolved


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("deck_name", nargs="?", help="Exact deck name (see --list)")
    parser.add_argument("--maybeboard", action="store_true", help="Append maybeboard as a separate section")
    parser.add_argument("--list", action="store_true", help="List all deck names and exit")
    parser.add_argument("-o", "--output", help="Write to a file instead of stdout")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if args.list or not args.deck_name:
        list_decks(conn)
        if not args.deck_name:
            sys.exit(0 if args.list else 1)

    lines, unresolved = export_deck(conn, args.deck_name, args.maybeboard)
    output = "\n".join(lines)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output + "\n")
        print(f"Wrote {len(lines)} lines to {args.output}")
    else:
        print(output)

    if unresolved:
        print(f"\n⚠ {len(unresolved)} cards could not be resolved:", file=sys.stderr)
        for u in unresolved:
            print(f"   - {u}", file=sys.stderr)


if __name__ == "__main__":
    main()
