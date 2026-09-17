"""
Export your collection to Moxfield's Collection CSV import format
(a completely separate Moxfield feature from export_moxfield.py's deck
paste format — this one uploads a .csv of everything you own).

One row per printing + foil status you own, quantities summed across any
collection lots sharing that exact printing (separate lots are kept for
price/date history elsewhere in the app, but Moxfield's own CSV expects
one row per printing/foil, not one row per lot). Untracked bulk lots
(no counted quantity, e.g. a pile of unsorted basics) are excluded.

This is also available directly in the dashboard (Collection page —
"Export to Moxfield" expander), which shares the exact same formatting
logic via dashboard_lib/moxfield_export.py so the two never drift apart.
This CLI script remains useful for scripting/automation outside the
dashboard.

Usage:
    python export_moxfield_collection.py                    # full collection -> stdout
    python export_moxfield_collection.py --not-in-deck       # owned, but not run in any deck
    python export_moxfield_collection.py -o collection.csv
"""
import os
import sys
import sqlite3
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")

sys.path.insert(0, BASE_DIR)
from dashboard_lib import moxfield_export


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--not-in-deck", action="store_true",
        help="Only printings with zero copies used in any deck's mainboard",
    )
    parser.add_argument("-o", "--output", help="Write to a file instead of stdout")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    csv_text = moxfield_export.export_collection_csv_text(conn, only_not_in_deck=args.not_in_deck)
    row_count = csv_text.count("\n") - 1  # minus the header line

    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as f:
            f.write(csv_text)
        print(f"Wrote {row_count} row(s) to {args.output}")
    else:
        sys.stdout.write(csv_text)


if __name__ == "__main__":
    main()
