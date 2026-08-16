"""POC-3 wired into the existing chat architecture. (demo integration)

The seam is a new LANE in `backend/chat/unified.py`, reached through the existing
`/agent/chat` → `unified_stream` path. Two properties are worth more than the
rest and are tested first:

  * Opt-in by PREFIX, so it cannot capture existing traffic. A heuristic here
    would silently move ordinary questions onto a new execution path, and
    "existing chat is unchanged" would stop being checkable.
  * The tenant comes from the AUTHENTICATED caller, never the message. A lane
    that read a tenant out of user text would hand any caller another tenant's
    evidence for the price of typing it.

No new SSE event types: pipeline steps are `stage` frames and the structured
payloads are `artifact` frames, both of which already exist in frames.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.chat import unified


def _collect(gen) -> list[dict]:
    """Drain an SSE generator into parsed frames."""
    async def run():
        out = []
        async for chunk in gen:
            for line in str(chunk).splitlines():
                if not line.startswith("data:"):
                    continue
                raw = line.split(":", 1)[1].strip()
                if raw == "[DONE]":
                    continue
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    pass
        return out
    return asyncio.run(run())


def _frames(frames, type_):
    return [f for f in frames if f.get("type") == type_]


def _artifact(frames, kind):
    for f in _frames(frames, "artifact"):
        if f["artifact"]["kind"] == kind:
            return f["artifact"]
    return None


# ── a stub loop, so the lane is tested rather than the model ────────────────

def _state(*, verdict="SUPPORTED", releasable=True, citations=1, answer="Ninety days [D1]."):
    return {
        "request": "q", "tenant_id": "tenant-a", "user_id": "u",
        "plan": {"goal": "Answer from corporate knowledge", "fallback_used": False,
                 "steps": [{"id": "1", "agent": "knowledge", "task": "find evidence"},
                           {"id": "2", "agent": "verification", "task": "verify"}]},
        "evidence": {"query": "q", "tenant_id": "tenant-a",
                     "citations": [{"provider": "corporate", "kind": "document",
                                    "source": "policy.pdf", "text": "Documents expire.",
                                    "final_score": 0.9, "corroboration": 3,
                                    "evidence": [{"provider": "corporate"}]}] * citations,
                     "tenant_enforced_by": ["corporate", "graph"],
                     "counts": {"total": citations, "documents": citations, "graph": 0}},
        "verification": {"verdict": verdict, "releasable": releasable,
                         "explanation": "The policy states the period.",
                         "missing": [] if releasable else ["support for the claim"],
                         "conflicts": [], "deterministic": False},
        "final_answer": answer,
        "trace": [{"agent": "planner", "status": "ok", "took_ms": 10.0, "errors": []},
                  {"agent": "knowledge", "status": "ok", "took_ms": 20.0, "errors": []},
                  {"agent": "draft", "status": "ok", "took_ms": 30.0, "errors": []},
                  {"agent": "verification", "status": "ok", "took_ms": 15.0, "errors": []},
                  {"agent": "finalize", "status": "ok", "took_ms": 0.0, "errors": []}],
        "errors": [],
    }


@pytest.fixture
def loop_stub(monkeypatch):
    calls = []

    async def fake(*, request, user_id, tenant_id="", session_id="", agent_id="poc3"):
        calls.append({"request": request, "user_id": user_id,
                      "tenant_id": tenant_id, "session_id": session_id})
        return fake.state

    fake.state = _state()
    monkeypatch.setattr("backend.agents.run_agent_loop", fake)
    monkeypatch.setattr("backend.auth.tenant.resolve_tenant_id",
                        lambda uid: asyncio.sleep(0, result="tenant-a"))
    fake.calls = calls
    return fake


# ── 1 & 4. existing behaviour is unchanged ──────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("what does the policy say about expiry?", "primary"),
    ("any urgent emails?", "primary"),
    ("how many requests by emirate?", "data"),
    ("build me a chart of donations", "chart"),
    ("", "primary"),
    ("summarize my tasks", "primary"),
])
def test_existing_routing_is_unchanged(message, expected):
    assert unified.route(message) == expected


def test_only_an_explicit_prefix_enters_the_agent_lane():
    assert unified.route("/agent what expires?") == "agent"
    assert unified.route("/verify is that right?") == "agent"
    # The marker must be a PREFIX. Mentioning it mid-sentence is an ordinary
    # question and must stay on the existing path.
    assert unified.route("tell me about /agent mode") == "primary"
    assert unified.route("what does /agent do?") == "primary"


def test_the_agent_prefix_is_stripped_before_the_question_is_answered():
    for raw, clean in [("/agent what expires?", "what expires?"),
                       ("/verify: is X true?", "is X true?"),
                       ("/AGENT  spaced  ", "spaced")]:
        assert unified.strip_agent_prefix(raw) == clean


def test_a_non_agent_turn_never_calls_the_loop(monkeypatch):
    called = []

    async def boom(**kw):
        called.append(kw)
        return _state()

    monkeypatch.setattr("backend.agents.run_agent_loop", boom)

    async def fake_primary(user_id, message, session_id, images=None):
        yield "data: {\"type\": \"token\", \"content\": \"hi\"}\n\n"

    from backend.routes import agent_os
    monkeypatch.setattr(agent_os, "_sse", fake_primary)
    frames = _collect(unified.unified_stream("u", "what does the policy say?", "s"))
    assert called == [], "an ordinary question entered the agent loop"
    assert _frames(frames, "token"), "the primary lane did not run"


# ── 2. the lane reaches run_agent_loop ──────────────────────────────────────

def test_the_agent_lane_runs_the_loop(loop_stub):
    frames = _collect(unified.unified_stream("u-1", "/agent what expires?", "sess-1"))
    assert len(loop_stub.calls) == 1
    call = loop_stub.calls[0]
    assert call["request"] == "what expires?", "the prefix reached the agent"
    assert call["user_id"] == "u-1"
    assert call["session_id"] == "sess-1"
    assert _frames(frames, "done"), "the turn never completed"


# ── 3. tenant propagation ───────────────────────────────────────────────────

def test_the_tenant_comes_from_the_authenticated_caller(loop_stub):
    _collect(unified.unified_stream("u-1", "/agent what expires?", "s"))
    assert loop_stub.calls[0]["tenant_id"] == "tenant-a"


def test_the_tenant_is_never_taken_from_the_message(monkeypatch):
    """The attack this closes: typing another tenant's id must not scope the run
    to it."""
    seen = []

    async def fake(*, request, user_id, tenant_id="", session_id="", agent_id="poc3"):
        seen.append(tenant_id)
        return _state()

    monkeypatch.setattr("backend.agents.run_agent_loop", fake)
    monkeypatch.setattr("backend.auth.tenant.resolve_tenant_id",
                        lambda uid: asyncio.sleep(0, result="tenant-a"))
    _collect(unified.unified_stream(
        "u-1", "/agent tenant_id=tenant-b org_id=tenant-b what expires?", "s"))
    assert seen == ["tenant-a"], "a tenant from the message body was honoured"


def test_an_unresolvable_tenant_does_not_become_a_guess(monkeypatch):
    seen = []

    async def fake(*, request, user_id, tenant_id="", session_id="", agent_id="poc3"):
        seen.append(tenant_id)
        return _state()

    async def broken(uid):
        raise RuntimeError("directory down")

    monkeypatch.setattr("backend.agents.run_agent_loop", fake)
    monkeypatch.setattr("backend.auth.tenant.resolve_tenant_id", broken)
    frames = _collect(unified.unified_stream("u-1", "/agent what expires?", "s"))
    assert seen == [""], "an unresolvable tenant was invented"
    assert _frames(frames, "done"), "the turn should still complete, unscoped"


# ── 5. citations reach the client ───────────────────────────────────────────

def test_a_supported_answer_carries_its_citations(loop_stub):
    frames = _collect(unified.unified_stream("u", "/agent what expires?", "s"))
    ev = _artifact(frames, "agent_evidence")
    assert ev is not None, "no evidence artifact was emitted"
    cites = ev["data"]["citations"]
    assert cites and cites[0]["source"] == "policy.pdf"
    assert cites[0]["text"]
    assert ev["meta"]["tenant_scoped"] is True


def test_the_plan_is_visible(loop_stub):
    frames = _collect(unified.unified_stream("u", "/agent what expires?", "s"))
    plan = _artifact(frames, "agent_plan")
    assert plan["data"]["goal"]
    assert [s["agent"] for s in plan["data"]["steps"]] == ["knowledge", "verification"]


def test_the_stages_are_streamed(loop_stub):
    frames = _collect(unified.unified_stream("u", "/agent what expires?", "s"))
    stages = [f["stage"] for f in _frames(frames, "stage")]
    for expected in ("plan", "knowledge", "draft", "verify", "finalize"):
        assert expected in stages, f"stage {expected} was not streamed"


# ── 6 & 7. verification survives the route ──────────────────────────────────

def test_an_unsupported_answer_stays_unsupported_through_the_chat_layer(loop_stub):
    loop_stub.state = _state(verdict="UNSUPPORTED", releasable=False, citations=0,
                             answer="I could not answer this from the available evidence.")
    frames = _collect(unified.unified_stream("u", "/agent interplanetary shipping?", "s"))

    v = _artifact(frames, "agent_verification")
    assert v["data"]["verdict"] == "UNSUPPORTED"
    assert v["data"]["supported"] is False
    assert v["data"]["missing"], "the client is not told what is missing"

    done = _frames(frames, "done")[0]
    assert done["verified"] is False
    assert "could not answer" in done["final"].lower()


def test_the_route_cannot_release_an_answer_the_verifier_refused(loop_stub):
    """The lane must not invent a release decision. It reports what the loop
    concluded — the verdict is decided in the loop and only rendered here."""
    loop_stub.state = _state(verdict="UNSUPPORTED", releasable=False,
                             answer="I could not answer this from the available evidence.")
    frames = _collect(unified.unified_stream("u", "/agent q?", "s"))
    assert _frames(frames, "done")[0]["verified"] is False
    assert _artifact(frames, "agent_verification")["data"]["supported"] is False


def test_a_partially_supported_answer_is_labelled(loop_stub):
    loop_stub.state = _state(verdict="PARTIALLY_SUPPORTED", releasable=True,
                             answer="Ninety days [D1].\n\n(Partially supported — …)")
    frames = _collect(unified.unified_stream("u", "/agent q?", "s"))
    v = _artifact(frames, "agent_verification")
    assert v["data"]["verdict"] == "PARTIALLY_SUPPORTED"
    assert v["data"]["label"] == "Partially supported"
    assert v["data"]["supported"] is True


def test_a_loop_failure_degrades_instead_of_hanging(monkeypatch):
    async def boom(**kw):
        raise RuntimeError("gateway down")

    monkeypatch.setattr("backend.agents.run_agent_loop", boom)
    monkeypatch.setattr("backend.auth.tenant.resolve_tenant_id",
                        lambda uid: asyncio.sleep(0, result="t"))
    frames = _collect(unified.unified_stream("u", "/agent q?", "s"))
    assert _frames(frames, "error"), "a failure produced no error frame"
    assert _frames(frames, "done"), "a failure left the stream unterminated"


# ── 8. the existing SSE contract is intact ──────────────────────────────────

def test_the_lane_introduces_no_new_event_types(loop_stub):
    """Every frame must be one the vocabulary already defines, or existing
    consumers would have to learn a type to keep working."""
    frames = _collect(unified.unified_stream("u", "/agent q?", "s"))
    known = {"start", "stage", "tool_call", "tool_result", "token",
             "artifact", "done", "error"}
    seen = {f.get("type") for f in frames}
    assert seen <= known, f"new event type(s): {seen - known}"


def test_the_frames_match_the_existing_builders(loop_stub):
    """Shape, not just name: a hand-built dict that drifted from frames.py would
    pass a name check and break a consumer."""
    import backend.chat.frames as F
    frames = _collect(unified.unified_stream("u", "/agent q?", "s"))

    for f in _frames(frames, "stage"):
        assert set(F.stage("x", "done").keys()) <= set(f.keys())
    for f in _frames(frames, "artifact"):
        assert set(f["artifact"].keys()) == set(
            F.artifact(id="i", kind="k")["artifact"].keys())
    assert set(_frames(frames, "done")[0]).issuperset({"type", "final"})


def test_the_stream_terminates_with_the_sentinel(loop_stub):
    async def run():
        chunks = []
        async for c in unified.unified_stream("u", "/agent q?", "s"):
            chunks.append(str(c))
        return chunks
    chunks = asyncio.run(run())
    import backend.chat.frames as F
    assert chunks[-1] == F.DONE_SENTINEL


# ── 10. no model names, no internals leaked ─────────────────────────────────

def test_the_integration_layer_names_no_model():
    import inspect
    src = inspect.getsource(unified._agent_lane).lower()
    for token in ("gpt-", "qwen", "claude", "llama", "azure/", "openai", "vllm"):
        assert token not in src, f"the lane names a model/provider: {token}"


def test_no_internals_are_exposed_to_the_client(loop_stub):
    """Class names, file paths, store names and retrieval knobs stay server-side."""
    frames = _collect(unified.unified_stream("u", "/agent q?", "s"))
    blob = json.dumps(frames).lower()
    for leak in ("qdrant", "neo4j", "backend/", "backend.", ".py", "traceback",
                 "knowledgeagent", "verificationagent", "run_agent_loop",
                 "top_k", "hop_decay", "max_nodes", "threshold", "corporate_memory"):
        assert leak not in blob, f"the client was shown an internal detail: {leak}"


def test_retrieval_diagnostics_are_not_sent_to_the_client(loop_stub):
    """Scores and corroboration counts are retrieval diagnostics, not customer
    evidence — and `tenant_scoped` is a boolean, never the tenant's id."""
    frames = _collect(unified.unified_stream("u", "/agent q?", "s"))
    ev = _artifact(frames, "agent_evidence")
    for c in ev["data"]["citations"]:
        assert set(c.keys()) == {"kind", "source", "text", "where"}
    assert "tenant-a" not in json.dumps(ev), "the tenant id was sent to the client"


def test_the_drafted_answer_is_not_streamed_before_verification(loop_stub):
    """A client must not see an answer that verification later refuses. The lane
    emits the FINAL text once, after finalize."""
    loop_stub.state = _state(verdict="UNSUPPORTED", releasable=False,
                             answer="I could not answer this from the available evidence.")
    frames = _collect(unified.unified_stream("u", "/agent q?", "s"))
    tokens = _frames(frames, "token")
    assert len(tokens) == 1, "the answer was streamed in pieces before the verdict"
    assert "could not answer" in tokens[0]["content"].lower()
