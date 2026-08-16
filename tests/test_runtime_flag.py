"""P0-B: the runtime migration switch.

The property that matters most is the FIRST one: with no environment set, nobody
is on Runtime B. Every other behaviour here is opt-in, so a deploy that forgets to
set anything is a deploy that changes nothing.

Precedence is asserted explicitly because it is the part an operator relies on
during an incident: the deny list has to beat a percentage rollout, or "get this
user off Runtime B now" would not work.
"""
import pytest

from backend import runtime_flag as rf

ENV_VARS = ("RUNTIME_B_ENABLED", "RUNTIME_B_PERCENT", "RUNTIME_B_USERS",
            "RUNTIME_B_SESSIONS", "RUNTIME_B_DENY_USERS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every case starts from an unset environment — otherwise a leaked variable
    from one test silently decides another."""
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    yield


# ── default: nothing moves ────────────────────────────────────────────────────
def test_default_is_runtime_a_for_everyone():
    for uid in ("sub-1", "sub-2", "alice@x.test", "", None):
        c = rf.choose(user_id=uid, session_id="s")
        assert c.runtime == "A"
        assert c.reason == "default"
    assert rf.use_runtime_b("anyone") is False


def test_snapshot_reports_a_disabled_rollout_by_default():
    snap = rf.snapshot()
    assert snap["enabled"] is False
    assert snap["percent"] == 0
    assert snap["pinned_users"] == [] and snap["pinned_sessions"] == []
    assert snap["default_runtime"] == "A" and snap["target_runtime"] == "B"


# ── pinning ───────────────────────────────────────────────────────────────────
def test_pinned_session_is_the_narrowest_cohort(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_SESSIONS", "sess-canary")
    assert rf.choose(user_id="anyone", session_id="sess-canary").is_b
    assert rf.choose(user_id="anyone", session_id="sess-canary").reason == "pinned_session"
    # Everything else stays on A.
    assert not rf.choose(user_id="anyone", session_id="sess-other").is_b


def test_pinned_user_by_uid_and_by_email(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_USERS", "sub-alice, alice@x.test")
    assert rf.choose(user_id="sub-alice").is_b
    assert rf.choose(user_id="x", email="alice@x.test").is_b
    assert not rf.choose(user_id="sub-bob").is_b


def test_pinning_is_case_insensitive_and_whitespace_tolerant(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_USERS", "  Sub-Alice  ,   ")
    assert rf.choose(user_id="sub-alice").is_b
    assert rf.choose(user_id="SUB-ALICE").is_b


# ── precedence ────────────────────────────────────────────────────────────────
def test_deny_list_beats_every_opt_in(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_B_PERCENT", "100")
    monkeypatch.setenv("RUNTIME_B_USERS", "sub-alice")
    monkeypatch.setenv("RUNTIME_B_SESSIONS", "sess-1")
    monkeypatch.setenv("RUNTIME_B_DENY_USERS", "sub-alice")
    c = rf.choose(user_id="sub-alice", session_id="sess-1")
    assert c.runtime == "A"
    assert c.reason == "deny_user"


def test_session_pin_beats_user_pin(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_SESSIONS", "sess-1")
    monkeypatch.setenv("RUNTIME_B_USERS", "sub-alice")
    assert rf.choose(user_id="sub-alice", session_id="sess-1").reason == "pinned_session"


def test_user_pin_beats_percentage(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_USERS", "sub-alice")
    monkeypatch.setenv("RUNTIME_B_PERCENT", "1")
    assert rf.choose(user_id="sub-alice").reason == "pinned_user"


# ── percentage rollout ────────────────────────────────────────────────────────
def test_percentage_is_stable_for_the_same_user(monkeypatch):
    """A user must not flip between runtimes mid-conversation — that would split
    their history across two stores."""
    monkeypatch.setenv("RUNTIME_B_PERCENT", "50")
    first = rf.choose(user_id="sub-alice").runtime
    for _ in range(50):
        assert rf.choose(user_id="sub-alice").runtime == first


def test_percentage_bucket_survives_a_process_restart():
    """blake2b, not Python's salted hash(): the same identity must land in the same
    bucket in a different process, or a canary would reshuffle on every deploy."""
    assert rf._bucket("sub-alice") == rf._bucket("sub-alice")
    assert rf._bucket("sub-alice") == 55  # pinned expectation, not a tautology


def test_percentage_zero_moves_nobody(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_PERCENT", "0")
    assert not any(rf.choose(user_id=f"sub-{i}").is_b for i in range(200))


def test_percentage_hundred_moves_everyone_with_an_identity(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_PERCENT", "100")
    assert all(rf.choose(user_id=f"sub-{i}").is_b for i in range(200))


def test_percentage_rollout_is_roughly_proportional(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_PERCENT", "25")
    ids = [f"sub-{i}" for i in range(1000)]
    moved = sum(1 for i in ids if rf.choose(user_id=i).is_b)
    assert 200 <= moved <= 300, moved


def test_anonymous_identity_is_never_in_a_partial_rollout(monkeypatch):
    """No identity means no stable bucket; such a caller stays on A rather than
    landing somewhere arbitrary."""
    monkeypatch.setenv("RUNTIME_B_PERCENT", "99")
    assert not rf.choose(user_id="").is_b
    assert not rf.choose(user_id=None).is_b


# ── global switch + rollback ──────────────────────────────────────────────────
def test_global_switch_moves_everyone(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_ENABLED", "true")
    c = rf.choose(user_id="sub-anyone")
    assert c.is_b and c.reason == "global"


@pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
def test_global_switch_off_values(monkeypatch, value):
    monkeypatch.setenv("RUNTIME_B_ENABLED", value)
    assert not rf.choose(user_id="sub-anyone").is_b


def test_rollback_is_one_variable(monkeypatch):
    """Unsetting the rollout returns everyone to Runtime A immediately — no deploy,
    no code change, no state to unwind."""
    monkeypatch.setenv("RUNTIME_B_PERCENT", "100")
    assert rf.choose(user_id="sub-alice").is_b
    monkeypatch.setenv("RUNTIME_B_PERCENT", "0")
    assert not rf.choose(user_id="sub-alice").is_b


def test_malformed_percent_fails_closed(monkeypatch):
    for bad in ("abc", "-5", "", "1e3"):
        monkeypatch.setenv("RUNTIME_B_PERCENT", bad)
        c = rf.choose(user_id="sub-alice")
        assert c.runtime == "A", f"{bad!r} moved traffic"


def test_percent_above_100_is_clamped_not_rejected(monkeypatch):
    monkeypatch.setenv("RUNTIME_B_PERCENT", "500")
    assert rf.choose(user_id="sub-alice").is_b


# ── the bridge is wired in, and only there ────────────────────────────────────
def test_chat_endpoint_consults_the_flag_before_anything_else():
    """The bridge must run before Runtime A's bookkeeping, or a Runtime B turn
    writes half its state through the wrong stores."""
    import inspect
    from backend import main
    src = inspect.getsource(main.chat_endpoint)
    flag_at = src.index("runtime_flag.choose")
    # The CALL, not the comment that explains the ordering.
    persist_at = src.index("_persist_turn(request.session_id")
    assert flag_at < persist_at


def test_bridge_does_not_fall_back_to_runtime_a_on_failure():
    """A silent fallback would hide every parity defect behind a retry on the old
    engine, making the canary meaningless."""
    import inspect
    from backend import main
    src = inspect.getsource(main._serve_via_runtime_b)
    assert "chat_endpoint" not in src
    assert "x-runtime" in src  # the response says which engine served it
