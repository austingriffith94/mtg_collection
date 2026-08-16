"""
MTG Dashboard — launcher.

Double-click run.bat (Windows) / run.command (Mac) / run.sh (Linux),
or just run `python run.py` / `python3 run.py` from a terminal —
after activating whatever conda/venv environment you're managing
yourself (see requirements.txt / README for the two packages needed:
pandas and requests). This script does NOT install anything on its
own; it just checks what's importable in your current environment
and tells you what's missing if something is.

Handles running the migration, exporting a deck to Moxfield format,
syncing the local image cache, and — once built — launching the
Streamlit dashboard.
"""
import os
import sys
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.py")  # doesn't exist yet — see run_dashboard()

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
    print("\nRunning migration — this hits the Scryfall API for each unique "
          "printing and can take a few minutes.\n")
    subprocess.run([sys.executable, "migrate.py"], cwd=SCRIPTS_DIR)


def run_export():
    # pure stdlib — no dependency check needed
    deck = input("Deck name (exact — leave blank to see the full list): ").strip()
    args = [sys.executable, "export_moxfield.py"]
    args.append(deck if deck else "--list")
    subprocess.run(args, cwd=SCRIPTS_DIR)


def run_image_sync():
    if not require("requests", label="image sync"):
        return
    subprocess.run([sys.executable, "sync_images.py"], cwd=SCRIPTS_DIR)


def run_dashboard():
    if not os.path.exists(DASHBOARD_PATH):
        print("\nThe Streamlit dashboard hasn't been built yet — that's the next phase "
              "of the project. For now, use the migration and Moxfield export below.\n")
        return
    if not require("streamlit", label="the dashboard"):
        return
    subprocess.run([sys.executable, "-m", "streamlit", "run", "dashboard.py"], cwd=BASE_DIR)


def main():
    check_python_version()

    while True:
        db_exists = os.path.exists(DB_PATH)
        print("=" * 50)
        print("MTG Collection Dashboard")
        print(f"(using: {sys.executable})")
        print("=" * 50)
        if not db_exists:
            print("No database found yet — run the migration first (option 1).\n")

        print("1) Run / refresh database migration (rebuilds from CSVs in data/)")
        print("2) Launch dashboard" + ("" if db_exists else "  [needs migration first]"))
        print("3) Export a deck to Moxfield format" + ("" if db_exists else "  [needs migration first]"))
        print("4) Sync image cache (download new + prune unused)" + ("" if db_exists else "  [needs migration first]"))
        print("5) Exit")
        choice = input("> ").strip()

        if choice == "1":
            run_migration()
        elif choice == "2":
            if not db_exists:
                print("Run the migration first (option 1).\n")
                continue
            run_dashboard()
        elif choice == "3":
            if not db_exists:
                print("Run the migration first (option 1).\n")
                continue
            run_export()
        elif choice == "4":
            if not db_exists:
                print("Run the migration first (option 1).\n")
                continue
            run_image_sync()
        elif choice == "5":
            break
        else:
            print("Not a valid option.\n")
        print()


if __name__ == "__main__":
    main()
