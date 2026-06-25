"""Unit tests for the action guardrail policy."""
import importlib

import backend.guardrails as g


def _reload_with(level, monkeypatch):
    monkeypatch.setenv("AUTONOMY_LEVEL", level)
    return importlib.reload(g)


def test_unknown_action_denied():
    assert g.decide("rm_rf_everything") == "deny"
    assert g.decide("send_money") == "deny"


def test_reads_always_auto():
    assert g.decide("get_analytics") == "auto"


def test_standard_level(monkeypatch):
    gg = _reload_with("standard", monkeypatch)
    assert gg.decide("create_task") == "auto"
    assert gg.decide("draft_email") == "approval"
    assert gg.decide("schedule_meeting") == "approval"


def test_assist_level_gates_tasks(monkeypatch):
    gg = _reload_with("assist", monkeypatch)
    assert gg.decide("create_task") == "approval"
    assert gg.decide("draft_email") == "approval"


def test_autonomous_level(monkeypatch):
    gg = _reload_with("autonomous", monkeypatch)
    assert gg.decide("create_task") == "auto"
    assert gg.decide("draft_email") == "auto"


def test_policy_snapshot_shape(monkeypatch):
    gg = _reload_with("standard", monkeypatch)
    snap = gg.policy_snapshot()
    assert snap["autonomy_level"] == "standard"
    assert "create_task" in snap["actions"]


def teardown_module(module):
    # restore default for the rest of the suite
    import os
    os.environ.pop("AUTONOMY_LEVEL", None)
    importlib.reload(g)
