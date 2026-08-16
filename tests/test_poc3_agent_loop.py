"""POC-3 acceptance: one real agentic execution loop.

    request → plan → knowledge → draft → verify → finalize

The seven scenarios from the brief. The agents are REAL — the planner parses a
real model response, the knowledge agent calls the real `knowledge_search`, the
verifier applies its real rules. Only two things are substituted:

  * the model gateway (`orchestrator.llm.chat`), so a test asserts on what the
    loop DOES with a response rather than on today's model weights; and
  * the Qdrant/Neo4j leaves, via scratch fixtures, so tenant isolation is
    asserted against known documents.

Everything between those two edges — plan parsing, step dropping, evidence
shaping, the deterministic verification floor, the release decision — is the
code that ships. The `_GatewayStub` counts calls, so a test cannot pass because
the loop quietly stopped calling the model.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from backend import ingest
from backend.agents import (AgentRequest, AgentStatus, Evidence, KnowledgeAgent,
                            PlannerAgent, Verdict, VerificationAgent,
                            default_plan, run_agent_loop)
from backend.agents import loop as loop_mod
from backend.agents import planner as planner_mod
from backend.agents import verification_agent as verify_mod
from backend.context.providers import registry as prov_registry

TENANT_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
DIM = 384


# ── gateway stub: one place every model call is answered and counted ─────────

class _GatewayStub:
    def __init__(self, plan=None, draft=None, verdict=None):
        self.calls: list[dict] = []
        self._plan = plan
        self._draft = draft if draft is not None else "Documents expire after 90 days [D1]."
        self._verdict = verdict

    async def chat(self, messages, tools=None, **kw):
        agent = (kw.get("ctx") or {}).get("agent_id", "?")
        self.calls.append({"agent": agent, "tier": kw.get("tier"),
                           "temperature": kw.get("temperature"), "kw": kw})
        if agent == "planner":
            return {"content": self._plan if self._plan is not None else json.dumps({
                "goal": "Answer from corporate knowledge",
                "steps": [{"id": "1", "agent": "knowledge", "task": "find policy evidence"},
                          {"id": "2", "agent": "verification", "task": "verify"}]})}
        if agent == "draft":
            return {"content": self._draft}
        if agent == "verification":
            return {"content": self._verdict if self._verdict is not None else json.dumps({
                "verdict": "SUPPORTED", "explanation": "The policy states the period.",
                "supporting": ["[D1]"], "missing": [], "conflicts": []})}
        return {"content": ""}

    def called_by(self, agent: str) -> int:
        return sum(1 for c in self.calls if c["agent"] == agent)


@pytest.fixture
def gateway(monkeypatch):
    stub = _GatewayStub()
    from backend.orchestrator import llm
    monkeypatch.setattr(llm, "chat", stub.chat)
    return stub


# ── scratch corpus + graph, so isolation is asserted on known documents ──────

def _qdrant_up() -> bool:
    try:
        import httpx
        return httpx.get(f"{os.getenv('QDRANT_URL', 'http://127.0.0.1:6333')}/collections",
                         timeout=3).status_code == 200
    except Exception:
        return False


class _V:
    def tolist(self):
        return [1.0] + [0.0] * (DIM - 1)


@pytest.fixture
def corpus(monkeypatch):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    col = f"poc3_{uuid.uuid4().hex[:8]}"
    c = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"), timeout=30)
    c.create_collection(col, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    vec = [1.0] + [0.0] * (DIM - 1)
    c.upsert(col, points=[
        PointStruct(id=1, vector=vec, payload={
            "user_id": "__org__", "org_id": TENANT_A, "source": "alpha-policy.pdf",
            "document_id": "doc-a",
            "text": "ALPHASECRET zirconium documents expire ninety days after issue."}),
        PointStruct(id=2, vector=vec, payload={
            "user_id": "__org__", "org_id": TENANT_B, "source": "beta-policy.pdf",
            "document_id": "doc-b",
            "text": "BETASECRET tungsten permits lapse after thirty days."}),
        PointStruct(id=3, vector=vec, payload={
            "user_id": "__org__", "org_id": ingest.SHARED_TENANT, "source": "handbook.pdf",
            "document_id": "doc-shared",
            "text": "SHAREDNOTE the records office is open on weekdays."}),
    ], wait=True)
    monkeypatch.setattr(ingest, "RAG_COLLECTION", col)
    monkeypatch.setattr(ingest, "get_client", lambda: c)
    monkeypatch.setattr(ingest, "get_embedder",
                        lambda: type("E", (), {"embed": lambda self, xs: [_V()]})())
    try:
        yield col
    finally:
        c.delete_collection(col)


class _GraphProvider:
    """A real ContextProvider emitting tenant-stamped graph items through the
    real GraphRetrievalAPI predicate."""
    name = "graph"
    timeout = 5.0
    enabled = True

    def is_applicable(self, request):
        return True

    async def collect(self, request):
        from backend.context.bundle import ContextItem
        from backend.knowledge_graph.retrieval import api as kg_api
        from backend.knowledge_graph.retrieval.types import ScoredEdge, ScoredNode, Subgraph

        class _R:
            def __init__(s, eid): s.entity_id, s.resolved, s.mention = eid, True, eid

        class _Res:
            def resolve(s, q, allow_fuzzy=True):
                return ([_R("alpha-doc-rule"), _R("beta-doc-rule")],
                        {"extract_ms": 0.0, "resolve_ms": 0.0,
                         "mentions_found": 2, "mentions_resolved": 2})

        class _Ret:
            """Each tenant gets a seed AND a one-hop neighbour, so `related` is
            populated the way a real expansion populates it. The cross-tenant
            edge exists precisely so the test can prove it is dropped."""
            queries = 1
            def expand(s, ids, depth=None, rel_types=None):
                def node(eid, name, tenant, hop):
                    return ScoredNode(entity_id=eid, canonical_name=name,
                                      labels=["Entity"], hop=hop, score=0.9 - 0.1 * hop,
                                      properties={"org_id": tenant})
                a = node("alpha-doc-rule", "AlphaExpiryRule", TENANT_A, 0)
                a_rel = node("alpha-retention", "AlphaRetentionSchedule", TENANT_A, 1)
                b = node("beta-doc-rule", "BetaExpiryRule", TENANT_B, 0)
                b_rel = node("beta-retention", "BetaRetentionSchedule", TENANT_B, 1)
                sg = Subgraph(nodes=[a, a_rel, b, b_rel],
                              seed_ids=["alpha-doc-rule", "beta-doc-rule"])
                sg.edges = [
                    ScoredEdge(rel_type="GOVERNS", start_id="alpha-doc-rule",
                               end_id="alpha-retention", score=0.8),
                    ScoredEdge(rel_type="GOVERNS", start_id="beta-doc-rule",
                               end_id="beta-retention", score=0.8),
                    # Must never survive for either tenant — one endpoint is
                    # always invisible.
                    ScoredEdge(rel_type="RELATED_TO", start_id="alpha-doc-rule",
                               end_id="beta-doc-rule", score=0.7),
                ]
                return sg

        api = kg_api.GraphRetrievalAPI(resolver=_Res(), retriever=_Ret())
        ctx = await asyncio.to_thread(api.retrieve, request.message, depth=1, top_k=8,
                                      tenant_id=(request.tenant_id or None))
        out = []
        for n in list(ctx.related) + list(ctx.resolved):
            # Fall back to the id: `resolved` holds resolver objects, which carry
            # no canonical_name. An empty text would be dropped as an empty item
            # by the builder and the evidence would silently vanish.
            name = getattr(n, "canonical_name", "") or getattr(n, "entity_id", "")
            out.append(ContextItem(
                text=name, provider="graph", source="graph",
                score=getattr(n, "score", 0.0),
                metadata={"kind": "entity", "entity_id": getattr(n, "entity_id", "")}))
        for e in ctx.subgraph.edges:
            out.append(ContextItem(
                text=f"{e.start_id} —{e.rel_type}→ {e.end_id}", provider="graph",
                source="graph", score=e.score,
                metadata={"kind": "relationship", "rel_type": e.rel_type,
                          "start_id": e.start_id, "end_id": e.end_id}))
        return out


@pytest.fixture
def wired(corpus):
    from backend.context.providers.corporate import CorporateKnowledgeProvider
    import backend.context.builder as builder_mod

    saved = dict(prov_registry._REGISTRY)
    saved_builder = builder_mod._default
    prov_registry.clear()
    prov_registry.register(CorporateKnowledgeProvider(top_k=5), replace=True)
    prov_registry.register(_GraphProvider(), replace=True)
    builder_mod._default = None
    try:
        yield
    finally:
        builder_mod._default = saved_builder
        prov_registry.clear()
        for _n, p in saved.items():
            prov_registry.register(p, replace=True)


pytestmark = pytest.mark.skipif(not _qdrant_up(), reason="needs a live Qdrant")


def _run(**kw):
    return asyncio.run(run_agent_loop(**kw))


# ── 1. simple knowledge question ─────────────────────────────────────────────

def test_a_knowledge_question_runs_the_whole_loop(wired, gateway):
    st = _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)

    assert [t["agent"] for t in st["trace"]] == \
        ["planner", "knowledge", "draft", "verification", "finalize"]
    assert st["plan"]["steps"], "no plan was produced"
    assert st["evidence"]["counts"]["total"] > 0
    assert st["verification"]["verdict"] == Verdict.SUPPORTED.value
    assert "ninety days" in st["final_answer"] or "[D1]" in st["final_answer"]

    # Real execution: the model was actually consulted at each stage.
    assert gateway.called_by("planner") == 1
    assert gateway.called_by("draft") == 1
    assert gateway.called_by("verification") == 1


# ── 2. graph question ────────────────────────────────────────────────────────

def test_a_graph_question_returns_graph_evidence(wired, gateway):
    st = _run(request="What governs document expiry?", user_id="__org__",
              tenant_id=TENANT_A)
    ev = st["evidence"]
    assert ev["counts"]["graph"] > 0, "no Neo4j/graph evidence reached the loop"
    assert ev["counts"]["documents"] > 0, "no Qdrant evidence reached the loop"
    assert set(ev["tenant_enforced_by"]) == {"corporate", "graph"}


# ── 3. unsupported question — no fabricated answer ───────────────────────────

def test_an_unsupported_question_is_refused(wired, monkeypatch):
    """No evidence ⇒ UNSUPPORTED by a rule, the draft step is skipped, and the
    model is never asked to answer. It cannot hallucinate what it is not asked."""
    stub = _GatewayStub()
    from backend.orchestrator import llm
    monkeypatch.setattr(llm, "chat", stub.chat)

    async def nothing(*a, **k):
        return []
    monkeypatch.setattr(_GraphProvider, "collect", nothing)
    monkeypatch.setattr("backend.ingest.search_corporate", lambda *a, **k: [])

    st = _run(request="What is our policy on interplanetary shipping?",
              user_id="__org__", tenant_id=TENANT_A)

    assert st["evidence"]["counts"]["total"] == 0
    assert st["verification"]["verdict"] == Verdict.UNSUPPORTED.value
    assert st["verification"]["deterministic"] is True
    assert stub.called_by("draft") == 0, "the model was asked to draft with no evidence"
    assert "could not answer" in st["final_answer"].lower()
    assert next(t for t in st["trace"] if t["agent"] == "draft")["status"] == \
        AgentStatus.SKIPPED.value


def test_a_verifier_outage_fails_closed(wired, monkeypatch):
    """An unavailable verifier must not become a pass."""
    stub = _GatewayStub(verdict="not json at all")
    from backend.orchestrator import llm
    monkeypatch.setattr(llm, "chat", stub.chat)
    st = _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)
    assert st["verification"]["verdict"] == Verdict.UNSUPPORTED.value
    assert "could not answer" in st["final_answer"].lower()


def test_the_model_cannot_claim_support_without_evidence():
    """The clamp. Reached only when the model contradicts its own input."""
    v = asyncio.run(VerificationAgent().run(AgentRequest(
        task="verify", user_id="u", context={"evidence": Evidence(query="q"), "draft": "x"})))
    assert v.output.verdict is Verdict.UNSUPPORTED
    assert v.output.deterministic is True


# ── 4. tenant isolation, through the whole loop ──────────────────────────────

def test_tenant_a_never_receives_tenant_b_evidence(wired, gateway):
    st = _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)
    blob = json.dumps(st["evidence"])
    assert "ALPHASECRET" in blob, "tenant A did not get its own document"
    assert "SHAREDNOTE" in blob, "tenant A did not get shared data"
    assert "BETASECRET" not in blob, "TENANT A RECEIVED TENANT B'S DOCUMENT"
    assert "beta-doc-rule" not in blob, "TENANT A RECEIVED TENANT B'S GRAPH ENTITY"


def test_tenant_b_never_receives_tenant_a_evidence(wired, gateway):
    st = _run(request="When do permits lapse?", user_id="__org__", tenant_id=TENANT_B)
    blob = json.dumps(st["evidence"])
    assert "BETASECRET" in blob and "SHAREDNOTE" in blob
    assert "ALPHASECRET" not in blob, "TENANT B RECEIVED TENANT A'S DOCUMENT"
    assert "alpha-doc-rule" not in blob, "TENANT B RECEIVED TENANT A'S GRAPH ENTITY"


def test_the_tenant_survives_every_hop_to_the_evidence(wired, gateway):
    st = _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)
    assert st["tenant_id"] == TENANT_A
    assert st["evidence"]["tenant_id"] == TENANT_A


def test_a_run_with_no_tenant_is_not_starved(wired, gateway):
    """System/eval callers pass no tenant: no predicate, not an empty one."""
    st = _run(request="When do documents expire?", user_id="__org__")
    assert st["evidence"]["counts"]["total"] > 0
    assert st["evidence"]["tenant_enforced_by"] == []


# ── 5. citation preservation ─────────────────────────────────────────────────

def test_every_answer_retains_its_evidence(wired, gateway):
    st = _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)
    cits = st["evidence"]["citations"]
    assert cits, "the answer was released with no citations"
    for c in cits:
        assert c.get("provider") in {"corporate", "graph"}
        assert c.get("text")
    docs = [c for c in cits if c["provider"] == "corporate"]
    assert any(c.get("document_id") or c.get("source") for c in docs), \
        "document provenance was lost between knowledge_search and the loop"


def test_provenance_is_carried_not_rebuilt(wired, gateway):
    """The loop must not invent its own citation shape — fields come from
    knowledge_search's KnowledgeCitation."""
    st = _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)
    c = st["evidence"]["citations"][0]
    for key in ("provider", "kind", "source", "final_score", "corroboration", "evidence"):
        assert key in c, f"citation lost the {key!r} field"


# ── 6. planner model abstraction ─────────────────────────────────────────────

def test_no_agent_names_a_model():
    """Changing the deployment's model map must not require editing agent code."""
    import inspect
    from backend.agents import knowledge_agent
    banned = ("gpt-", "qwen", "claude", "llama", "mistral", "gemini",
              "azure/", "openai", "ollama", "vllm", "AsyncOpenAI")
    for mod in (planner_mod, verify_mod, loop_mod, knowledge_agent):
        src = inspect.getsource(mod).lower()
        for token in banned:
            assert token.lower() not in src, f"{mod.__name__} names a model/provider: {token}"


def test_agents_request_a_tier_not_a_model(wired, gateway):
    _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)
    assert gateway.calls, "no gateway call was made"
    for call in gateway.calls:
        assert call["tier"], "a call was made with no capability profile"
        assert "model" not in call["kw"], "an agent passed an explicit model"


def test_every_model_call_goes_through_the_gateway():
    """No agent may import a provider SDK."""
    import inspect
    from backend.agents import knowledge_agent
    for mod in (planner_mod, verify_mod, loop_mod, knowledge_agent):
        src = inspect.getsource(mod)
        assert "import openai" not in src and "from openai" not in src
        assert "httpx.post" not in src and "requests.post" not in src


# ── 7. tool boundary ─────────────────────────────────────────────────────────

def _code_only(module) -> str:
    """Source with docstrings and comments stripped.

    Scanning raw source would match this module's OWN prose — its docstring
    explains which stores it must not reach, and naming them is the point of the
    explanation. That is the same trap `test_identity_normalization` documents,
    where a naive literal scan flagged the comments describing the bug it fixed.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and ast.get_docstring(node):
            node.body = node.body[1:]
    return ast.unparse(tree)


def test_the_knowledge_agent_cannot_reach_qdrant_or_neo4j_directly():
    """Structural, not aspirational: the executable code must not touch either
    store. knowledge_search is where POC-2's tenant predicate lives, so an agent
    that queried a store directly would be correctly scoped only by luck."""
    from backend.agents import knowledge_agent
    code = _code_only(knowledge_agent)
    for forbidden in ("qdrant_client", "neo4j", "backend.ingest",
                      "knowledge_graph", "search_corporate", "GraphRetrievalAPI"):
        assert forbidden not in code, \
            f"the knowledge agent reaches past knowledge_search: {forbidden}"
    assert "knowledge_search" in code, "the knowledge agent must use knowledge_search"


def test_the_import_scan_is_not_vacuous():
    """A stripper that returned nothing would make the scan above pass forever."""
    from backend.agents import knowledge_agent
    code = _code_only(knowledge_agent)
    assert "class KnowledgeAgent" in code and "async def _run" in code
    assert "The reason is not tidiness" not in code, "docstrings were not stripped"


def test_the_knowledge_agent_calls_knowledge_search_exactly_once(monkeypatch):
    calls = []

    async def fake(query, *, user_id, session_id="", tenant_id=""):
        calls.append({"query": query, "user_id": user_id, "tenant_id": tenant_id})
        from backend.orchestrator.knowledge import KnowledgeSearchResult
        return KnowledgeSearchResult(query=query, user_id=user_id, tenant_id=tenant_id)

    monkeypatch.setattr("backend.orchestrator.knowledge.knowledge_search", fake)
    asyncio.run(KnowledgeAgent().run(AgentRequest(
        task="q", user_id="u-1", tenant_id=TENANT_A)))
    assert calls == [{"query": "q", "user_id": "u-1", "tenant_id": TENANT_A}]


# ── planner behaviour ────────────────────────────────────────────────────────

def test_the_planner_uses_the_models_plan_when_it_parses(gateway):
    r = asyncio.run(PlannerAgent().run(AgentRequest(task="q", user_id="u")))
    assert r.output.fallback_used is False, "the model's plan was ignored"
    assert [s.agent for s in r.output.steps] == ["knowledge", "verification"]


def test_the_planner_drops_steps_naming_an_unknown_agent(monkeypatch):
    stub = _GatewayStub(plan=json.dumps({"goal": "g", "steps": [
        {"id": "1", "agent": "shell", "task": "rm -rf /"},
        {"id": "2", "agent": "knowledge", "task": "find it"}]}))
    from backend.orchestrator import llm
    monkeypatch.setattr(llm, "chat", stub.chat)
    r = asyncio.run(PlannerAgent().run(AgentRequest(task="q", user_id="u")))
    assert [s.agent for s in r.output.steps] == ["knowledge"], "an invented agent survived"


def test_the_planner_is_bounded(monkeypatch):
    steps = [{"id": str(i), "agent": "knowledge", "task": f"t{i}"} for i in range(50)]
    stub = _GatewayStub(plan=json.dumps({"goal": "g", "steps": steps}))
    from backend.orchestrator import llm
    monkeypatch.setattr(llm, "chat", stub.chat)
    r = asyncio.run(PlannerAgent().run(AgentRequest(task="q", user_id="u")))
    assert len(r.output.steps) <= planner_mod.MAX_STEPS


def test_the_planner_falls_back_rather_than_failing(monkeypatch):
    stub = _GatewayStub(plan="I'm afraid I can't do that.")
    from backend.orchestrator import llm
    monkeypatch.setattr(llm, "chat", stub.chat)
    r = asyncio.run(PlannerAgent().run(AgentRequest(task="q", user_id="u")))
    assert r.status is AgentStatus.OK
    assert r.output.fallback_used is True
    assert r.errors, "a silent fallback would hide a broken planner"
    assert [s.agent for s in r.output.steps] == ["knowledge", "verification"]


# ── the loop's own invariant ─────────────────────────────────────────────────

def test_a_draft_is_never_released_without_a_supporting_verdict(wired, monkeypatch):
    """The edge POC-3 exists for. The model drafts a confident answer and the
    verifier rejects it; the draft must not reach the user."""
    stub = _GatewayStub(
        draft="Documents never expire, ever.",
        verdict=json.dumps({"verdict": "UNSUPPORTED",
                            "explanation": "The evidence says ninety days.",
                            "supporting": [], "missing": ["support for 'never expire'"],
                            "conflicts": []}))
    from backend.orchestrator import llm
    monkeypatch.setattr(llm, "chat", stub.chat)

    st = _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)
    # Assert on the WHOLE drafted sentence. A bare "never expire" also appears in
    # the refusal, because the refusal quotes what the evidence failed to
    # support — which is the refusal doing its job, not a leak.
    assert "Documents never expire, ever." not in st["final_answer"], \
        "an unsupported draft was released"
    assert "could not answer" in st["final_answer"].lower()
    assert st["trace"][-1]["released"] is False
    assert st["verification"]["verdict"] == Verdict.UNSUPPORTED.value


def test_a_partially_supported_answer_is_released_with_its_caveat(wired, monkeypatch):
    stub = _GatewayStub(verdict=json.dumps({
        "verdict": "PARTIALLY_SUPPORTED", "explanation": "Only the period is evidenced.",
        "supporting": ["[D1]"], "missing": ["the renewal process"], "conflicts": []}))
    from backend.orchestrator import llm
    monkeypatch.setattr(llm, "chat", stub.chat)
    st = _run(request="When do documents expire and how are they renewed?",
              user_id="__org__", tenant_id=TENANT_A)
    assert "Partially supported" in st["final_answer"]
    assert "renewal process" in st["final_answer"]


def test_the_state_traces_the_whole_run_without_secrets(wired, gateway):
    st = _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)
    for key in ("request", "tenant_id", "plan", "current_step", "evidence",
                "verification", "final_answer", "trace", "errors"):
        assert key in st, f"state cannot trace {key}"
    blob = json.dumps({k: v for k, v in st.items() if k != "messages"}).lower()
    for secret in ("api_key", "authorization", "bearer ", "password", "secret_key"):
        assert secret not in blob, f"state leaked {secret}"


# ── the declining-draft floor (found by live integration validation) ─────────

def test_a_declining_draft_is_unsupported_not_supported(wired, monkeypatch):
    """Live validation produced SUPPORTED on a refusal: the verifier is asked
    "does the evidence support this answer?", and a draft saying "the evidence
    does not mention this" IS supported by the absence of evidence. A UI would
    then put a green "Supported" badge over "I don't know"."""
    stub = _GatewayStub(
        draft="The provided evidence does not mention interplanetary shipping insurance.",
        verdict=json.dumps({"verdict": "SUPPORTED",
                            "explanation": "The evidence indeed does not mention it.",
                            "supporting": ["[D1]"], "missing": [], "conflicts": []}))
    from backend.orchestrator import llm
    monkeypatch.setattr(llm, "chat", stub.chat)

    st = _run(request="What is our interplanetary shipping policy?",
              user_id="__org__", tenant_id=TENANT_A)

    assert st["verification"]["verdict"] == Verdict.UNSUPPORTED.value
    assert st["verification"]["deterministic"] is True
    assert st["verification"]["releasable"] is False
    assert "could not answer" in st["final_answer"].lower()


@pytest.mark.parametrize("draft,declines", [
    ("The evidence does not mention shipping.", True),
    ("It is not possible to determine the policy.", True),
    ("There is no information about that in the evidence.", True),
    ("I could not answer this from the available evidence.", True),
    ("Documents expire ninety days after issue [D1].", False),
    ("The policy covers shipping but does not mention insurance rates.", True),
])
def test_the_decline_detector_errs_toward_withholding(draft, declines):
    """The last case is a deliberate false positive: a partly-answering draft
    that mentions a gap is withheld. Withholding a real answer is recoverable;
    badging a refusal as supported is not."""
    from backend.agents.verification_agent import _draft_declines
    assert _draft_declines(draft) is declines


def test_the_decline_floor_does_not_fire_on_evidence_text(wired, gateway):
    """Evidence may legitimately discuss what a policy does not cover. Only the
    DRAFT is inspected."""
    st = _run(request="When do documents expire?", user_id="__org__", tenant_id=TENANT_A)
    assert st["verification"]["verdict"] == Verdict.SUPPORTED.value
    assert st["verification"]["deterministic"] is False
