"""Unit tests for analytics (backend/analytics.py) over a temp sqlite db."""
import sqlite3
import backend.analytics as analytics


def _make_db(path):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE tasks (id TEXT, title TEXT, source TEXT, status TEXT,
                 priority TEXT, due_date TEXT, notes TEXT, reminder_sent INTEGER,
                 created_at TEXT, updated_at TEXT, user_id TEXT)""")
    rows = [
        ("1", "Done task", "chat", "done", "medium", None, "", 0, "2026-06-20T10:00:00", "2026-06-22T10:00:00", "user_1"),
        ("2", "Pending urgent", "chat", "pending", "urgent", None, "", 0, "2026-06-21T10:00:00", "2026-06-21T10:00:00", "user_1"),
        ("3", "Pending high", "chat", "pending", "high", None, "", 0, "2026-06-21T10:00:00", "2026-06-21T10:00:00", "user_1"),
        ("4", "Other user", "chat", "pending", "low", None, "", 0, "2026-06-21T10:00:00", "2026-06-21T10:00:00", "user_2"),
    ]
    c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()


def test_summary(monkeypatch, tmp_path):
    db = tmp_path / "t.db"; _make_db(db)
    monkeypatch.setattr(analytics, "DB_PATH", str(db))
    r = analytics.run_metric("summary", "user_1")
    assert r["total"] == 3 and r["pending"] == 2 and r["done"] == 1
    assert r["pending_by_priority"] == {"urgent": 1, "high": 1}


def test_completed_window(monkeypatch, tmp_path):
    db = tmp_path / "t.db"; _make_db(db)
    monkeypatch.setattr(analytics, "DB_PATH", str(db))
    r = analytics.run_metric("completed", "user_1", days=3650)
    assert r["count"] == 1 and "Done task" in r["titles"]


def test_unknown_metric_rejected(monkeypatch, tmp_path):
    db = tmp_path / "t.db"; _make_db(db)
    monkeypatch.setattr(analytics, "DB_PATH", str(db))
    r = analytics.run_metric("DROP TABLE", "user_1")
    assert "error" in r and "available" in r


def test_user_scoping(monkeypatch, tmp_path):
    db = tmp_path / "t.db"; _make_db(db)
    monkeypatch.setattr(analytics, "DB_PATH", str(db))
    r = analytics.run_metric("summary", "user_2")
    assert r["total"] == 1
