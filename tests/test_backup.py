"""dashboard_lib/backup.py: consistent snapshots, retention, pre-op hooks,
and the integrity check. Uses file-backed temp databases (an in-memory one
is deliberately skipped)."""
import os
import sqlite3

from dashboard_lib import backup, writes


def _make_db(tmp_path):
    path = str(tmp_path / "mtg_collection.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    return conn, path


def test_snapshot_is_readable_copy(tmp_path):
    conn, path = _make_db(tmp_path)
    out = backup.snapshot(conn, "manual")
    assert out and os.path.dirname(out) == backup.backup_dir_for(path)
    assert sqlite3.connect(out).execute("SELECT x FROM t").fetchall() == [(1,)]
    assert not [f for f in os.listdir(backup.backup_dir_for(path)) if f.endswith(".part")]


def test_in_memory_db_is_skipped():
    assert backup.snapshot(sqlite3.connect(":memory:"), "manual") is None


def test_daily_snapshot_only_once_a_day(tmp_path):
    conn, _ = _make_db(tmp_path)
    assert backup.snapshot(conn, "daily")
    assert backup.snapshot(conn, "daily") is None


def test_before_snapshot_is_throttled(tmp_path):
    conn, _ = _make_db(tmp_path)
    assert backup.snapshot(conn, "before", "delete-deck")
    assert backup.snapshot(conn, "before", "delete-deck") is None
    assert backup.snapshot(conn, "before", "other")  # throttle is per operation


def test_retention_keeps_newest(tmp_path, monkeypatch):
    conn, path = _make_db(tmp_path)
    monkeypatch.setitem(backup.KEEP, "manual", 2)
    d = backup.backup_dir_for(path)
    os.makedirs(d)
    for i in range(4):
        p = os.path.join(d, f"mtg_collection_manual_old{i}.db")
        open(p, "w").close()
        os.utime(p, (1000 + i, 1000 + i))
    backup.snapshot(conn, "manual")
    left = sorted(os.listdir(d))
    assert len(left) == 2 and "mtg_collection_manual_old0.db" not in left


def test_integrity_and_latest_good(tmp_path):
    conn, path = _make_db(tmp_path)
    assert backup.integrity_problems(path) == []
    good = backup.snapshot(conn, "manual")
    with open(path, "wb") as f:
        f.write(b"not a database" * 100)
    assert backup.integrity_problems(path)
    assert backup.latest_good_snapshot(path) == good


def test_delete_deck_takes_a_snapshot(tmp_path):
    path = str(tmp_path / "mtg_collection.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(open(os.path.join(os.path.dirname(__file__), "..", "schema.sql"), encoding="utf-8").read())
    conn.execute("INSERT INTO decks (name) VALUES ('Gisa')")
    conn.commit()
    deck_id = conn.execute("SELECT deck_id FROM decks").fetchone()[0]
    name, err, *_ = writes.delete_deck(conn, deck_id)
    assert err is None
    assert any("before-delete-deck" in f for f in os.listdir(backup.backup_dir_for(path)))
