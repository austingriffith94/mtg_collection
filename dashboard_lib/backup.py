"""Database snapshots and integrity checks.

mtg_collection.db is gitignored and is the only copy of the collection, so
this module keeps dated, consistent copies of it in backups/ next to the
database. Snapshots use SQLite's online backup API (Connection.backup), so
they are consistent even while the dashboard is open and writing — unlike a
plain file copy (or a file-level backup tool) that can catch a write
half-done.

Pure stdlib, no Streamlit: used by run.py (menu option, once-a-day
snapshot at launch, before 'migrate') and by writes.py (before a deck
delete or a collection prune). In-memory databases are skipped, so tests
that use them are unaffected.

Three kinds of snapshot, each with its own retention:
  daily   backups/mtg_collection_daily_YYYY-MM-DD.db   keep newest 14
  manual  backups/mtg_collection_manual_<timestamp>.db  keep newest 10
  before  backups/mtg_collection_before-<what>_<timestamp>.db  keep newest 10
          (one per `what` at most every PRE_OP_MIN_GAP_SECONDS, so a loop of
          deletes doesn't write dozens of copies)
"""
import datetime
import glob
import logging
import os
import sqlite3
import time

logger = logging.getLogger(__name__)

BACKUP_DIRNAME = "backups"
KEEP = {"daily": 14, "manual": 10, "before": 10}
PRE_OP_MIN_GAP_SECONDS = 600


def _db_file(conn):
    """Path of the file behind `conn`, or None for an in-memory/temp DB."""
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main":
            return path or None
    return None


def backup_dir_for(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), BACKUP_DIRNAME)


def _prune(backup_dir, kind):
    """Delete all but the newest KEEP[kind] snapshots of this kind."""
    files = sorted(glob.glob(os.path.join(backup_dir, f"mtg_collection_{kind}*.db")),
                   key=os.path.getmtime, reverse=True)
    for old in files[KEEP[kind]:]:
        try:
            os.remove(old)
        except OSError:
            logger.warning("Could not remove old backup %s", old)


def _write_snapshot(src_conn, dest_path):
    """Copy src_conn's database to dest_path via the backup API, written to a
    temp name first so a crash never leaves a truncated file that looks real."""
    tmp = dest_path + ".part"
    dest = sqlite3.connect(tmp)
    try:
        src_conn.backup(dest)
    finally:
        dest.close()
    os.replace(tmp, dest_path)


def snapshot(conn, kind="manual", what=""):
    """Snapshot the database behind `conn`. Returns the new file's path, or
    None if skipped (in-memory DB, a daily/pre-op one that already exists
    or is too recent) or if it failed — a backup problem is logged but never
    allowed to break the action that asked for it."""
    try:
        db_path = _db_file(conn)
        if not db_path:
            return None
        out_dir = backup_dir_for(db_path)
        os.makedirs(out_dir, exist_ok=True)
        now = datetime.datetime.now()
        if kind == "daily":
            name = f"mtg_collection_daily_{now:%Y-%m-%d}.db"
        elif kind == "before":
            prefix = f"mtg_collection_before-{what}_"
            recent = glob.glob(os.path.join(out_dir, prefix + "*.db"))
            if recent and time.time() - max(map(os.path.getmtime, recent)) < PRE_OP_MIN_GAP_SECONDS:
                return None
            name = f"{prefix}{now:%Y-%m-%d_%H%M%S}.db"
        else:
            name = f"mtg_collection_manual_{now:%Y-%m-%d_%H%M%S}.db"
        dest = os.path.join(out_dir, name)
        if kind == "daily" and os.path.exists(dest):
            return None
        _write_snapshot(conn, dest)
        _prune(out_dir, kind)
        logger.info("Database snapshot written: %s", dest)
        return dest
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception("Database snapshot failed (%s %s)", kind, what)
        return None


def snapshot_path(db_path, kind="manual", what=""):
    """snapshot() for callers holding a path, not a connection (run.py)."""
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        return snapshot(conn, kind, what)
    finally:
        conn.close()


def integrity_problems(db_path):
    """[] if SQLite's integrity_check and foreign_key_check both come back
    clean, else a list of readable problem lines (an unreadable file is one
    line too)."""
    problems = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for (msg,) in conn.execute("PRAGMA integrity_check"):
                if msg != "ok":
                    problems.append(f"integrity_check: {msg}")
            for row in conn.execute("PRAGMA foreign_key_check"):
                problems.append("foreign_key_check: table %s row %s -> %s" % (row[0], row[1], row[2]))
        finally:
            conn.close()
    except sqlite3.Error as e:
        problems.append(f"could not read database: {e}")
    return problems


def latest_good_snapshot(db_path):
    """Newest snapshot in backups/ that passes integrity_problems(), or None."""
    files = sorted(glob.glob(os.path.join(backup_dir_for(db_path), "mtg_collection_*.db")),
                   key=os.path.getmtime, reverse=True)
    for f in files:
        if not integrity_problems(f):
            return f
    return None
