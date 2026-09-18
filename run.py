"""
MTG Dashboard — launcher.

Double-click run.bat (Windows) / run.command (Mac) / run.sh (Linux),
or just run `python run.py` / `python3 run.py` from a terminal —
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
"""
import os
import sys
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")

MIN_PYTHON = (3, 8)


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


def run_dashboard():
    if not require("streamlit", label="the dashboard"):
        return
    subprocess.run([sys.executable, "-m", "streamlit", "run", "dashboard.py"], cwd=BASE_DIR)


def main():
    check_python_version()

    while True:
        db_exists = os.path.exists(DB_PATH)
        needs_migration_tag = "" if db_exists else "  [needs the initial migration first — see below]"
        print("=" * 50)
        print("MTG Collection Dashboard")
        print(f"(using: {sys.executable})")
        print("=" * 50)
        if not db_exists:
            print("No database found yet — type 'migrate' below to run the initial "
                  "migration from your CSVs in data/.\n")

        print("1) Launch dashboard" + needs_migration_tag)
        print("2) Export a deck to Moxfield format" + needs_migration_tag)
        print("3) Export collection to Moxfield CSV" + needs_migration_tag)
        print("4) Sync image cache (download new + prune unused)" + needs_migration_tag)
        print("5) Refresh Scryfall card data (Reserved List/Game Changer/legality/price)" + needs_migration_tag)
        print("6) Exit")
        print()
        print("Rarely needed once you're up and running — type the word, not a number:")
        print("  migrate) Rebuild the database from CSVs in data/ "
              "(⚠ discards any dashboard-made edits since your last migration)")
        choice = input("> ").strip()
        choice_lower = choice.lower()

        if choice_lower == "migrate":
            run_migration()
        elif choice == "1":
            if not db_exists:
                print("Type 'migrate' first to set up the database.\n")
                continue
            run_dashboard()
        elif choice == "2":
            if not db_exists:
                print("Type 'migrate' first to set up the database.\n")
                continue
            run_export()
        elif choice == "3":
            if not db_exists:
                print("Type 'migrate' first to set up the database.\n")
                continue
            run_export_collection()
        elif choice == "4":
            if not db_exists:
                print("Type 'migrate' first to set up the database.\n")
                continue
            run_image_sync()
        elif choice == "5":
            if not db_exists:
                print("Type 'migrate' first to set up the database.\n")
                continue
            run_refresh_card_data()
        elif choice == "6":
            break
        else:
            print("Not a valid option.\n")
        print()


if __name__ == "__main__":
    main()
