#!/usr/bin/env python3
"""
MTG Dashboard — one-stop launcher.

Double-click run.bat (Windows) / run.command (Mac) / run.sh (Linux),
or just run `python run.py` / `python3 run.py` from a terminal.

Handles first-time setup (virtual environment + dependencies), running
the migration, exporting a deck to Moxfield format, and — once built —
launching the Streamlit dashboard.
"""
import os
import sys
import venv
import hashlib
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(BASE_DIR, ".venv")
REQUIREMENTS = os.path.join(BASE_DIR, "requirements.txt")
REQ_HASH_MARKER = os.path.join(VENV_DIR, ".requirements_hash")
DB_PATH = os.path.join(BASE_DIR, "mtg_collection.db")
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.py")  # doesn't exist yet — see run_dashboard()


def venv_python():
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def requirements_hash():
    with open(REQUIREMENTS, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def ensure_venv():
    """Create the venv once, and re-install dependencies whenever
    requirements.txt changes (e.g. when the dashboard adds streamlit
    later) — not just on the very first run."""
    first_time = not os.path.exists(venv_python())
    if first_time:
        print("First-time setup: creating a virtual environment (.venv)...")
        venv.create(VENV_DIR, with_pip=True)

    need_install = first_time
    if not need_install:
        if not os.path.exists(REQ_HASH_MARKER):
            need_install = True
        else:
            with open(REQ_HASH_MARKER) as f:
                need_install = f.read().strip() != requirements_hash()

    if need_install:
        print("Installing dependencies (only happens on first run or when they change)...")
        try:
            subprocess.run(
                [venv_python(), "-m", "pip", "install", "-q", "-r", REQUIREMENTS],
                check=True,
            )
        except subprocess.CalledProcessError:
            print("\n⚠ Dependency install failed — check your internet connection and try again.")
            sys.exit(1)
        with open(REQ_HASH_MARKER, "w") as f:
            f.write(requirements_hash())
        print("Setup complete.\n")


def run_migration():
    print("\nRunning migration — this hits the Scryfall API for each unique "
          "printing and can take a few minutes.\n")
    subprocess.run([venv_python(), "migrate.py"], cwd=SCRIPTS_DIR)


def run_export():
    deck = input("Deck name (exact — leave blank to see the full list): ").strip()
    args = [venv_python(), "export_moxfield.py"]
    args.append(deck if deck else "--list")
    subprocess.run(args, cwd=SCRIPTS_DIR)


def run_image_sync():
    subprocess.run([venv_python(), "sync_images.py"], cwd=SCRIPTS_DIR)


def run_dashboard():
    if not os.path.exists(DASHBOARD_PATH):
        print("\nThe Streamlit dashboard hasn't been built yet — that's the next phase "
              "of the project. For now, use the migration and Moxfield export below.\n")
        return
    subprocess.run([venv_python(), "-m", "streamlit", "run", "dashboard.py"], cwd=BASE_DIR)


def main():
    ensure_venv()

    while True:
        db_exists = os.path.exists(DB_PATH)
        print("=" * 50)
        print("MTG Collection Dashboard")
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
