"""
One-time copy of the functional labels from the `strategy` tag layer into
`function` (docs/TAG_SUGGESTIONS_PLAN.md Phase 1b). `strategy` rows are not
touched, and a copied tag is never added twice, so this is safe to re-run.

    python scripts/copy_function_tags.py           # show what would be copied
    python scripts/copy_function_tags.py --apply   # back up, then copy
"""
import collections
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")

sys.path.insert(0, BASE_DIR)
from dashboard_lib import queries as q
from dashboard_lib import writes


def main():
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH}.")
        sys.exit(1)
    conn = q.open_connection(DB_PATH)  # also applies any pending schema upgrades
    rows = q.pending_function_tag_copy(conn)
    print(f"{len(rows)} tag(s) to copy into function:")
    for label, count in collections.Counter(r["label"] for r in rows).most_common():
        print(f"  {label}: {count}")
    if "--apply" not in sys.argv[1:]:
        print("Nothing changed. Run with --apply to copy them.")
        return
    backup_path, copied = writes.copy_strategy_tags_to_function(conn)
    print(f"Copied {copied} tag(s). Backup: {backup_path or 'none needed'}")


if __name__ == "__main__":
    main()
