"""
MTG Dashboard — launcher.

Double-click run.bat (Windows) / run.command (Mac) / run.sh (Linux),
or just run `python run.py` / `python3 run.py` from a terminal
(add `--dashboard` to skip the menu and launch the app straight away) —
after activating whatever conda/venv environment you're managing
yourself (see requirements.txt / README for the two packages needed:
pandas and requests). This script does NOT install anything on its
own; it just checks what's importable in your current environment
and tells you what's missing if something is.

Handles launching the Streamlit dashboard, exporting a deck or your
collection to Moxfield format, syncing the local image cache, and
refreshing Scryfall-derived card fields (Reserved List/Game
Changer/legality/price) without a full migration. The one-time/rare CSV
migration is intentionally NOT a numbered menu option — it's destructive
(wipes and rebuilds mtg_collection.db, discarding any dashboard-made
edits) so it's tucked behind typing the word 'migrate' instead of a
number, with its own confirmation prompt before it actually runs.

The database is also protected by dashboard_lib/backup.py: an integrity
check and a once-a-day snapshot (backups/) whenever the dashboard is
launched from here, a "Back up the database now" menu option, and a
snapshot before 'migrate'.
"""
import os
import sys
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")

sys.path.insert(0, BASE_DIR)
from dashboard_lib import backup  # stdlib-only, safe to import before the env check

MIN_PYTHON = (3, 11)  # tomllib (advisor secrets) is 3.11+


def check_python_version():
    if sys.version_info < MIN_PYTHON:
        print(f"⚠ This needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ — "
              f"you're running {sys.version_info[0]}.{sys.version_info[1]}.")
        print("Switch to a newer environment and re-run.")
        sys.exit(1)


def require(*modules, label):
    """Check the given modules are importable in the CURRENT environment
    (whatever conda/venv you've activated) without installing anything.
    Returns True if all present; otherwise prints what's missing and
    how to install it, and returns False so the caller can bail out of
    just that one action."""
    missing = []
    for m in modules:
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    if missing:
        names = " ".join(missing)
        print(f"\n⚠ Missing package(s) needed for {label}: {', '.join(missing)}")
        print(f"  pip install {names}")
        print(f"  conda install {names}")
        print("Install into your active environment, then try again.\n")
        return False
    return True


def run_migration():
    if not require("pandas", "requests", label="the migration"):
        return
    print(
        "\n⚠ This deletes and rebuilds mtg_collection.db from scratch from the "
        "CSVs in data/ — it will silently discard any decks/collection/tags/cover "
        "images/etc. you've added or edited in the dashboard since your last "
        "migration. Only do this for your very first import, or if you actually "
        "want to throw away everything the dashboard itself has stored.\n"
    )
    confirm = input("Type REBUILD to confirm, or anything else to cancel: ").strip()
    if confirm != "REBUILD":
        print("Cancelled — nothing was touched.\n")
        return
    saved = backup.snapshot_path(DB_PATH, "before", "migrate")
    if saved:
        print(f"Backed up the current database first: {saved}")
    print("\nRunning migration — this hits the Scryfall API for each unique "
          "printing and can take a few minutes.\n")
    subprocess.run([sys.executable, "migrate.py"], cwd=SCRIPTS_DIR)


def run_export():
    # pure stdlib — no dependency check needed
    deck = input("Deck name (exact — leave blank to see the full list): ").strip()
    args = [sys.executable, "export_moxfield.py"]
    args.append(deck if deck else "--list")
    subprocess.run(args, cwd=SCRIPTS_DIR)


def run_export_collection():
    # pure stdlib — no dependency check needed
    scope = input("Which cards? [f]ull collection or [n]ot in a deck (default: full): ").strip().lower()
    args = [sys.executable, "export_moxfield_collection.py"]
    if scope.startswith("n"):
        args.append("--not-in-deck")
    out_file = input("Output file (leave blank to print to the terminal): ").strip()
    if out_file:
        args.extend(["-o", out_file])
    subprocess.run(args, cwd=SCRIPTS_DIR)


def run_image_sync():
    if not require("requests", label="image sync"):
        return
    subprocess.run([sys.executable, "sync_images.py"], cwd=SCRIPTS_DIR)


def run_refresh_card_data():
    if not require("requests", label="refreshing card data"):
        return
    print("\nRe-fetching every card by its Scryfall ID to refresh Reserved List / "
          "Game Changer / legality / price — this doesn't touch your collection, "
          "decklists, tags, or anything else.\n")
    subprocess.run([sys.executable, "refresh_card_data.py"], cwd=SCRIPTS_DIR)


def run_backup():
    saved = backup.snapshot_path(DB_PATH, "manual")
    if saved:
        print(f"\nBacked up to {saved}\n")
    else:
        print("\nBackup failed — see logs/ for details.\n")


def check_database_health():
    """Integrity-check the live database and take today's snapshot (once a
    day). Never blocks launching, but a damaged database is called out
    loudly along with the newest snapshot that is still good."""
    if not os.path.exists(DB_PATH):
        return
    problems = backup.integrity_problems(DB_PATH)
    if problems:
        print("\n⚠ DATABASE PROBLEM — mtg_collection.db failed its integrity check:")
        for p in problems[:5]:
            print(f"  {p}")
        good = backup.latest_good_snapshot(DB_PATH)
        print(f"  Newest good snapshot: {good or 'none found in backups/'}")
        print("  Not taking today's snapshot (it would copy the damage). "
              "See README: Backups.\n")
        return
    backup.snapshot_path(DB_PATH, "daily")


def run_dashboard():
    if not require("streamlit", label="the dashboard"):
        return
    check_database_health()
    # One-time, idempotent: the image cache moved from image_cache/ to
    # static/image_cache/ (UI plan Phase 4) so Streamlit can serve it.
    sys.path.insert(0, SCRIPTS_DIR)
    from move_image_cache import move_legacy_image_cache
    moved, _kept = move_legacy_image_cache()
    if moved:
        print(f"Moved {moved} cached image file(s) to static/image_cache/.")
    subprocess.run([sys.executable, "-m", "streamlit", "run", "dashboard.py"], cwd=BASE_DIR)


# Numbered menu: (label, function, needs_db). The Exit entry is appended by
# main() so it is always last. Migration is deliberately not here — it's
# destructive, so it's only reachable by typing the word 'migrate'.
MENU = [
    ("Launch dashboard", run_dashboard, True),
    ("Export a deck to Moxfield format", run_export, True),
    ("Export collection to Moxfield CSV", run_export_collection, True),
    ("Back up the database now", run_backup, True),
    ("Sync image cache (download new + prune unused)", run_image_sync, True),
    ("Refresh Scryfall card data (Reserved List/Game Changer/legality/price)",
     run_refresh_card_data, True),
]


def launch_dashboard_directly():
    """`python run.py --dashboard`: skip the menu and start the app."""
    if not os.path.exists(DB_PATH):
        print("No database found yet — run `python run.py` and type 'migrate' "
              "first.")
        sys.exit(1)
    run_dashboard()


def main():
    check_python_version()
    if "--dashboard" in sys.argv[1:]:
        launch_dashboard_directly()
        return

    while True:
        db_exists = os.path.exists(DB_PATH)
        # With no DB nothing but migrate/Exit can work, so don't list the rest.
        items = [(label, func) for label, func, needs_db in MENU
                 if db_exists or not needs_db]
        items.append(("Exit", None))

        print("=" * 50)
        print("MTG Collection Dashboard")
        print(f"(using: {sys.executable})")
        print("=" * 50)
        if not db_exists:
            print("No database found yet — type 'migrate' below to run the initial "
                  "migration from your CSVs in data/.\n")

        for n, (label, _func) in enumerate(items, start=1):
            print(f"{n}) {label}")
        print()
        print("Rarely needed once you're up and running — type the word, not a number:")
        print("  migrate) Rebuild the database from CSVs in data/ "
              "(⚠ discards any dashboard-made edits since your last migration)")
        choice = input("> ").strip()

        if choice.lower() == "migrate":
            run_migration()
        elif choice.isdigit() and 1 <= int(choice) <= len(items):
            func = items[int(choice) - 1][1]
            if func is None:
                break
            func()
        else:
            print("Not a valid option.\n")
        print()


if __name__ == "__main__":
    main()
