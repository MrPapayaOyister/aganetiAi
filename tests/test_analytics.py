"""Analytics metrics (backend/analytics.py).

These tests used to build a temp sqlite file and monkeypatch
`analytics.DB_PATH`. That constant was deleted when tasks moved to Postgres
(commit 2c62c20, "Phase D: tasks + events + analytics on Postgres"); the tests
were left behind and had been failing with AttributeError ever since — four
permanently-red tests that everyone learned to scroll past.

The logic they covered is still real and still worth covering: the metric
allowlist, the aggregation, and the fact that a user's id reaches the store.
What changed is only the SEAM. analytics reads through
`tasks.store.get_all_tasks`, so that is what these fake out — no database, no
backend-specific fixture, and nothing to go stale the next time the storage
layer moves.
"""

import pytest

import backend.analytics as analytics

#: One user's tasks, plus another user's, so a scoping mistake has something to
#: leak. Shaped like what get_all_tasks returns, not like a table row.
TASKS = {
    "user_1": [
        {"id": "1", "title": "Done task", "status": "done", "priority": "medium",
         "created_at": "2026-06-20T10:00:00", "updated_at": "2026-06-22T10:00:00"},
        {"id": "2", "title": "Pending urgent", "status": "pending", "priority": "urgent",
         "created_at": "2026-06-21T10:00:00", "updated_at": "2026-06-21T10:00:00"},
        {"id": "3", "title": "Pending high", "status": "pending", "priority": "high",
         "created_at": "2026-06-21T10:00:00", "updated_at": "2026-06-21T10:00:00"},
    ],
    "user_2": [
        {"id": "4", "title": "Other user", "status": "pending", "priority": "low",
         "created_at": "2026-06-21T10:00:00", "updated_at": "2026-06-21T10:00:00"},
    ],
}


@pytest.fixture
def store(monkeypatch):
    """Fake the store and RECORD the arguments it was called with.

    Recording matters: user scoping is enforced by the store, so the thing
    analytics is actually responsible for is passing the right user through. A
    test that only checked the returned counts would pass just as happily if the
    id were dropped on the floor.
    """
    calls: list[tuple] = []

    def get_all_tasks(user_id, status=None):
        calls.append((user_id, status))
        rows = TASKS.get(user_id, [])
        return [r for r in rows if status is None or r["status"] == status]

    import tasks.store as ts
    monkeypatch.setattr(ts, "get_all_tasks", get_all_tasks)
    return calls


# ── the allowlist ────────────────────────────────────────────────────────────

def test_an_unknown_metric_is_rejected_and_says_what_is_available():
    """The allowlist is the security boundary — this is exposed as a tool, and
    it must never become free-form SQL."""
    r = analytics.run_metric("DROP TABLE", "user_1")
    assert "error" in r and r["available"] == analytics.METRICS


@pytest.mark.parametrize("metric", analytics.METRICS)
def test_every_advertised_metric_actually_runs(metric, store):
    """`available` is a promise. A metric listed but unhandled would fall through
    to {"error": "unhandled metric"} — advertised and broken."""
    r = analytics.run_metric(metric, "user_1")
    assert r.get("error") != "unhandled metric", metric
    assert "human" in r, f"{metric} returned no human-readable summary"


# ── aggregation ──────────────────────────────────────────────────────────────

def test_summary_counts_and_splits_by_priority(store):
    r = analytics.run_metric("summary", "user_1")
    assert (r["total"], r["pending"], r["done"]) == (3, 2, 1)
    assert r["pending_by_priority"] == {"urgent": 1, "high": 1}


def test_completed_respects_the_window(store):
    wide = analytics.run_metric("completed", "user_1", days=3650)
    assert wide["count"] == 1 and "Done task" in wide["titles"]
    # The same data outside the window must report nothing, or the window is
    # decorative.
    narrow = analytics.run_metric("completed", "user_1", days=1)
    assert narrow["count"] == 0


def test_pending_by_priority_asks_the_store_for_pending_only(store):
    r = analytics.run_metric("pending_by_priority", "user_1")
    assert r["pending_by_priority"] == {"urgent": 1, "high": 1}
    assert ("user_1", "pending") in store, \
        "must filter at the store, not by pulling every task and discarding"


# ── scoping ──────────────────────────────────────────────────────────────────

def test_the_requested_user_is_the_only_user_asked_for(store):
    r = analytics.run_metric("summary", "user_2")
    assert r["total"] == 1
    assert {uid for uid, _ in store} == {"user_2"}, \
        "analytics asked the store for a user it was not given"


# ── failure is not emptiness ─────────────────────────────────────────────────

def test_a_store_failure_currently_reads_as_zero_tasks(monkeypatch):
    """PINNING KNOWN BEHAVIOUR, not endorsing it.

    `_tasks` swallows every exception and returns []. So a database that is
    down reports "0 pending, 0 done, 0 total tasks" — indistinguishable from a
    user who has nothing to do. That is the silent-success shape this codebase
    keeps getting bitten by (exit-code-0 conversions, status="indexed" with 0
    chunks, a dead OCR server returning 0 characters).

    Left as-is because changing it is a behaviour change that belongs in its own
    piece of work, but recorded here so it is a decision rather than an
    oversight.
    """
    def boom(user_id, status=None):
        raise RuntimeError("database is down")

    import tasks.store as ts
    monkeypatch.setattr(ts, "get_all_tasks", boom)
    r = analytics.run_metric("summary", "user_1")
    assert r["total"] == 0 and "error" not in r
