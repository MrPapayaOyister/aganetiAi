"""Phase 3 — the knowledge-graph tool in Runtime B.

Four claims, from the brief, each with tests:

  * the graph tool is callable through Runtime B (registered, authorized, executed
    by the real `_tools_node`);
  * unauthorized access is rejected;
  * tenant isolation is preserved;
  * GraphService remains the ONLY Cypher executor.

The last one is the easiest to lose by accident — someone adds a `cypher` argument
"just for debugging" and the model gains arbitrary read access to the graph. It is
therefore asserted structurally (against the tool schema and the module source) as
well as behaviourally.

Neo4j is stubbed for the unit cases. The live-graph checks are marked `neo4j` and
skip when the graph is unreachable, so the suite runs on a laptop.
"""
import asyncio
import inspect

import pytest

import backend.orchestrator  # noqa: F401 — registers graph_search
from backend.orchestrator import authz, graph_tools, registry
from backend.orchestrator.authz import Decision, RiskLevel

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Node:
    def __init__(self, nid, name):
        self.id = nid
        self.properties = {"id": nid, "canonical_name": name}


class _FakeService:
    """Records the arguments every call receives, so the tests can assert the
    tenant actually travelled rather than merely being accepted."""

    def __init__(self, entities=None, hops=None):
        self.calls = []
        self._entities = entities or []
        self._hops = hops or []

    def _owned(self, e, tenant_id):
        """Emulate the Cypher predicate: an entity belongs to exactly one tenant.
        Defaulting to "whoever asked" would make every isolation test vacuous."""
        return e.get("_tenant", TENANT_A) == tenant_id

    def tenant_find_entities(self, name, *, tenant_id, limit=10, prop=None, strict=False):
        self.calls.append(("find", name, tenant_id, prop, strict, limit))
        return [e for e in self._entities
                if e["node"].properties["canonical_name"].lower() == name.lower()
                and self._owned(e, tenant_id)]

    def tenant_search_entities(self, text, *, tenant_id, limit=10, prop=None, strict=False):
        self.calls.append(("search", text, tenant_id, prop, strict, limit))
        return [e for e in self._entities
                if text.lower() in e["node"].properties["canonical_name"].lower()
                and self._owned(e, tenant_id)]

    def tenant_search_entities_any_token(self, terms, *, tenant_id, limit=10,
                                         prop=None, strict=False):
        self.calls.append(("tokens", tuple(terms), tenant_id, prop, strict, limit))
        return [e for e in self._entities
                if any(t in e["node"].properties["canonical_name"].lower() for t in terms)
                and self._owned(e, tenant_id)]

    def tenant_expand_one_hop(self, ids, *, tenant_id, visited=None, limit=50,
                              prop=None, strict=False):
        self.calls.append(("expand", tuple(ids), tenant_id, prop, strict, limit))
        return [h for h in self._hops if h["from_id"] in ids]

    # Deliberately absent: run_query. If the tool ever reaches for it, these tests
    # fail with AttributeError rather than silently permitting arbitrary Cypher.


@pytest.fixture
def fake_graph(monkeypatch):
    svc = _FakeService(
        entities=[{"node": _Node("e1", "Agentic AI"), "labels": ["Project", "Entity"],
                   "degree": 9, "_tenant": TENANT_A}],
        hops=[{"from_id": "e1", "from_name": "Agentic AI", "node": _Node("e2", "Neo4j"),
               "labels": ["Technology"], "rel_type": "USES",
               "start_id": "e1", "end_id": "e2", "rel_props": {}}])
    monkeypatch.setattr("backend.knowledge_graph.service.get_graph_service", lambda: svc)
    monkeypatch.setattr("backend.knowledge_graph.client.is_enabled", lambda: True)
    return svc


# ══════════════════════════════════════════════════════════════════════════════
# Callable through Runtime B
# ══════════════════════════════════════════════════════════════════════════════
def test_graph_search_is_registered():
    t = registry.get("graph_search")
    assert t is not None
    assert t.required_permission == "knowledge.graph.read"
    assert t.is_outbound is False


def test_graph_search_is_classified():
    import backend.guardrails as g
    assert g.TOOL_CATEGORY["graph_search"] == "read"


def test_graph_search_is_allowed_with_a_grant():
    res = authz.authorize_call(user_id=USER, tenant_id=TENANT_A, agent_id="primary",
                               session_id="s", tool_name="graph_search",
                               arguments={"query": "Agentic AI"},
                               granted=["graph_search"], tool=registry.get("graph_search"))
    assert res.decision is Decision.ALLOW
    assert res.risk_level is RiskLevel.READ


def test_graph_search_runs_through_the_real_executor(fake_graph):
    """End to end through `_tools_node`: the tool is selected, authorized, and its
    handler executed by the LangGraph node — not called directly by the test."""
    from backend.orchestrator import graph as gmod
    state = {
        "messages": [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "graph_search",
                          "arguments": '{"query": "Agentic AI"}'}}]}],
        "user_id": USER, "tenant_id": TENANT_A, "session_id": "s",
        "agent_id": "primary", "board_id": "", "allowed_tools": ["graph_search"],
        "step": 1, "awaiting": None, "model_key": None, "fallback_models": [],
        "has_image": False,
    }
    out = _await(gmod._tools_node(state))
    body = out["messages"][0]["content"]
    assert "Agentic AI" in body
    assert "USES" in body and "Neo4j" in body
    assert out["awaiting"] is None


def test_permission_grant_also_reaches_it():
    res = authz.authorize_call(user_id=USER, tenant_id=TENANT_A, agent_id="p", session_id="s",
                               tool_name="graph_search", granted=["knowledge.graph.read"],
                               tool=registry.get("graph_search"))
    assert res.decision is Decision.ALLOW and res.rule == "grant:permission"


# ══════════════════════════════════════════════════════════════════════════════
# Unauthorized access is rejected
# ══════════════════════════════════════════════════════════════════════════════
def test_denied_without_a_grant_and_the_handler_never_runs(fake_graph):
    from backend.orchestrator import graph as gmod
    state = {
        "messages": [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "graph_search", "arguments": '{"query": "x"}'}}]}],
        "user_id": USER, "tenant_id": TENANT_A, "session_id": "s", "agent_id": "primary",
        "board_id": "", "allowed_tools": [], "step": 1, "awaiting": None,
        "model_key": None, "fallback_models": [], "has_image": False,
    }
    out = _await(gmod._tools_node(state))
    assert "not permitted" in out["messages"][0]["content"]
    assert fake_graph.calls == [], "graph was queried despite the refusal"


def test_denied_without_a_subject():
    res = authz.authorize_call(user_id="", tenant_id=TENANT_A, agent_id="p", session_id="s",
                               tool_name="graph_search", granted=["graph_search"],
                               tool=registry.get("graph_search"))
    assert res.decision is Decision.DENY and res.rule == "no_subject"


def test_kill_switch_disables_the_graph_tool(monkeypatch):
    import importlib
    import backend.guardrails as g
    monkeypatch.setenv("AGANETI_DENIED_TOOLS", "graph_search")
    importlib.reload(g)
    try:
        res = authz.authorize_call(user_id=USER, tenant_id=TENANT_A, agent_id="p",
                                   session_id="s", tool_name="graph_search",
                                   granted=["graph_search"], tool=registry.get("graph_search"))
        assert res.decision is Decision.DENY and res.rule == "kill_switch"
    finally:
        monkeypatch.delenv("AGANETI_DENIED_TOOLS", raising=False)
        importlib.reload(g)


# ══════════════════════════════════════════════════════════════════════════════
# Tenant isolation
# ══════════════════════════════════════════════════════════════════════════════
def test_no_tenant_is_refused_at_the_boundary():
    res = authz.authorize_call(user_id=USER, tenant_id="", agent_id="p", session_id="s",
                               tool_name="graph_search", granted=["graph_search"],
                               tool=registry.get("graph_search"))
    assert res.decision is Decision.DENY and res.rule == "no_tenant"


def test_handler_refuses_without_a_tenant_even_if_the_boundary_is_bypassed(fake_graph):
    """Defence in depth: a check that lives in one place only is one refactor away
    from living nowhere."""
    out = _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": "", "agent_id": "p"}, query="Agentic AI"))
    assert "requires a tenant context" in out
    assert fake_graph.calls == []


def test_structured_entry_point_raises_without_a_tenant():
    with pytest.raises(PermissionError):
        _await(graph_tools.graph_search("x", tenant_id=""))


def test_tenant_reaches_every_graph_call(fake_graph):
    """The point of P3 item 14: the tenant must arrive in the QUERY, not only in a
    log line. Every GraphService call made during one search carries it."""
    _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_A}, query="Agentic AI"))
    assert fake_graph.calls, "no graph call was made"
    for call in fake_graph.calls:
        assert call[2] == TENANT_A, f"{call[0]} did not carry the tenant"


def test_expansion_also_carries_the_tenant(fake_graph):
    _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_A}, query="Agentic AI", expand=True))
    expands = [c for c in fake_graph.calls if c[0] == "expand"]
    assert expands and all(c[2] == TENANT_A for c in expands)


def test_a_different_tenant_sees_nothing(fake_graph):
    """The fake honours the predicate the real Cypher applies."""
    out = _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_B}, query="Agentic AI"))
    # Tenant B's search still ran, scoped to B...
    assert all(c[2] == TENANT_B for c in fake_graph.calls)
    # ...and returned nothing, because the fixture's entity belongs to A.
    assert "No entity matching" in out


def test_result_records_the_tenant_scope_it_used(fake_graph):
    r = _await(graph_tools.graph_search("Agentic AI", tenant_id=TENANT_A))
    d = r.as_dict()
    assert d["tenant_id"] == TENANT_A
    assert d["tenant_property"] == graph_tools.TENANT_PROPERTY
    assert "tenant_strict" in d


def test_strict_mode_is_configurable_and_off_by_default():
    """Lenient by default (an unstamped node is shared) because the graph carries no
    tenant property yet; strict is available for a stamped graph."""
    assert graph_tools.TENANT_STRICT is False
    from backend.knowledge_graph import queries as q
    lenient = q.tenant_search_entities("org_id", False)
    strict = q.tenant_search_entities("org_id", True)
    assert "IS NULL OR" in lenient
    assert "IS NULL OR" not in strict
    assert "$tenant" in lenient and "$tenant" in strict


def test_expansion_filters_both_endpoints():
    """Filtering only the seed would let a hop walk out of the tenant and return the
    far node's properties."""
    from backend.knowledge_graph import queries as q
    cypher = q.tenant_expand_one_hop("org_id", True)
    assert cypher.count("$tenant") == 2, "only one endpoint is tenant-filtered"
    assert "a.org_id" in cypher and "b.org_id" in cypher


# ══════════════════════════════════════════════════════════════════════════════
# GraphService remains the only Cypher executor
# ══════════════════════════════════════════════════════════════════════════════
def test_the_model_cannot_supply_cypher():
    """The schema offers no parameter through which a statement, fragment, label or
    property name could be passed."""
    params = registry.get("graph_search").parameters
    assert set(params["properties"]) == {"query", "limit", "expand"}
    assert params["properties"]["query"]["type"] == "string"
    assert params["properties"]["limit"]["type"] == "integer"
    assert params["properties"]["expand"]["type"] == "boolean"
    blob = str(params).lower()
    for forbidden in ("cypher", "match ", "statement", "label", "property"):
        assert forbidden not in blob, f"schema mentions {forbidden!r}"


def _code_only(mod) -> str:
    """Module source with comments and the module docstring removed — these
    assertions are about CODE, and prose explaining why Cypher is forbidden must
    not itself trip the check."""
    src = inspect.getsource(mod)
    doc = inspect.getdoc(mod) or ""
    for line in doc.splitlines():
        src = src.replace(line, "")
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def test_no_cypher_is_authored_in_the_tool_module():
    code = _code_only(graph_tools)
    for kw in ("MATCH (", "RETURN ", "MERGE ", "CREATE ", "DETACH DELETE", "CALL db."):
        assert kw not in code, f"graph_tools.py contains Cypher: {kw!r}"


def test_the_escape_hatch_is_not_imported():
    """`GraphService.run_query` runs arbitrary statements. The tool layer must not
    reach it, and the fake service in this file does not define it — so any attempt
    would also fail loudly at runtime."""
    code = _code_only(graph_tools)
    assert "run_query" not in code
    assert not hasattr(_FakeService, "run_query")


def test_tool_only_calls_typed_tenant_methods(fake_graph):
    _await(graph_tools.graph_search("Agentic AI", tenant_id=TENANT_A))
    used = {c[0] for c in fake_graph.calls}
    assert used <= {"find", "search", "expand"}, f"unexpected graph API used: {used}"


def test_property_name_is_validated_before_interpolation():
    """The tenant property is interpolated (Cypher cannot parameterise a key), so it
    is constrained to the identifier grammar. It comes from configuration, never a
    request — but an operator typo must not become an injection."""
    from backend.knowledge_graph import queries as q
    assert q.validate_property_name("org_id") == "org_id"
    for hostile in ("x) RETURN 1 //", "a b", "", "1abc", "a-b", "n.id} MATCH (m"):
        with pytest.raises(ValueError):
            q.validate_property_name(hostile)


def test_every_value_in_the_tenant_queries_is_parameterised():
    from backend.knowledge_graph import queries as q
    for builder in (q.tenant_find_entities_by_name, q.tenant_search_entities,
                    q.tenant_expand_one_hop):
        cypher = builder()
        assert "$tenant" in cypher
        # No string literals: every value arrives as a parameter.
        assert "'" not in cypher and '"' not in cypher, f"{builder.__name__} embeds a literal"


# ══════════════════════════════════════════════════════════════════════════════
# Degradation
# ══════════════════════════════════════════════════════════════════════════════
def test_graph_disabled_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr("backend.knowledge_graph.client.is_enabled", lambda: False)
    out = _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_A}, query="x"))
    assert "not available" in out


def test_graph_unavailable_is_reported_not_raised(monkeypatch):
    from backend.knowledge_graph.client import GraphUnavailable

    class _Down:
        def tenant_find_entities(self, *a, **k):
            raise GraphUnavailable("neo4j is down")

    monkeypatch.setattr("backend.knowledge_graph.client.is_enabled", lambda: True)
    monkeypatch.setattr("backend.knowledge_graph.service.get_graph_service", lambda: _Down())
    out = _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_A}, query="x"))
    assert "not available" in out


def test_empty_query_asks_instead_of_scanning_the_graph(fake_graph):
    out = _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_A}, query="   "))
    assert "What should I look up" in out
    assert fake_graph.calls == []


def test_limit_is_clamped(fake_graph):
    _await(graph_tools.graph_search("Agentic AI", tenant_id=TENANT_A, limit=10_000))
    assert all(c[5] <= graph_tools.MAX_LIMIT for c in fake_graph.calls if c[0] != "expand")


def test_no_results_tells_the_model_not_to_invent(fake_graph):
    out = _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_A}, query="nothing-like-this-exists"))
    assert "Do not invent" in out


# ══════════════════════════════════════════════════════════════════════════════
# Live graph (skipped when Neo4j is unreachable)
# ══════════════════════════════════════════════════════════════════════════════
def _require_graph():
    """Skip at RUN time, not at import time.

    A module-level probe would call get_graph_service() during pytest COLLECTION,
    which builds the process-wide driver singleton before test_graph_retrieval.py
    has set up its own — and that test then cannot find its fixtures. Cost of the
    original version: four unrelated failures that only appeared in a full-suite
    run. Skip decisions belong inside the test.
    """
    try:
        from backend.knowledge_graph.client import is_enabled
        from backend.knowledge_graph.service import get_graph_service
        if is_enabled() and get_graph_service().ping():
            return
    except Exception:
        pass
    pytest.skip("Neo4j not reachable")


def test_live_graph_returns_entities_and_relationships():
    _require_graph()
    r = _await(graph_tools.graph_search("Agentic AI", tenant_id=TENANT_A, limit=5))
    assert r.available
    assert r.entities, "no entity found in the live graph"
    assert r.relationships, "no relationship expanded from the live graph"
    rendered = graph_tools.render_for_model(r)
    assert "ENTITIES:" in rendered and "RELATIONSHIPS:" in rendered


def test_live_strict_mode_excludes_unstamped_nodes():
    """The live graph carries no tenant property, so strict mode must return
    nothing — which is the evidence that the predicate is real Cypher and not a
    decoration."""
    _require_graph()
    from backend.knowledge_graph.service import get_graph_service
    svc = get_graph_service()
    lenient = svc.tenant_search_entities("Agentic", tenant_id=TENANT_A, limit=5, strict=False)
    strict = svc.tenant_search_entities("Agentic", tenant_id=TENANT_A, limit=5, strict=True)
    assert lenient, "lenient mode found nothing — fixture assumption broken"
    assert strict == [], "strict mode matched unstamped nodes"


# ══════════════════════════════════════════════════════════════════════════════
# Resolution: three passes, tightest first
# ══════════════════════════════════════════════════════════════════════════════
def test_qualified_query_still_resolves(fake_graph):
    """The live validation caught this: the model asks for "the Agentic AI project"
    when the node is called "Agentic AI". Exact and substring both miss, and
    answering "nothing is recorded" reads as an authoritative absence."""
    out = _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_A}, query="the Agentic AI project"))
    assert "Agentic AI" in out
    assert "No entity matching" not in out
    passes = [c[0] for c in fake_graph.calls]
    assert passes[:3] == ["find", "search", "tokens"], f"pass order wrong: {passes}"


def test_token_pass_is_only_reached_as_a_last_resort(fake_graph):
    """An exact hit must not be diluted by the loose pass."""
    _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_A}, query="Agentic AI"))
    assert "tokens" not in [c[0] for c in fake_graph.calls]


def test_stopwords_are_dropped_before_the_token_pass():
    assert graph_tools._terms("what is the Agentic AI project connected to") == ["agentic"]
    # "works" survives: it is a content word here, and over-trimming the stopword
    # list would start dropping real entity names.
    assert graph_tools._terms("who works on Neo4j?") == ["works", "neo4j"]
    # Nothing significant left -> no loose pass at all, rather than matching all.
    assert graph_tools._terms("what is connected to the project") == []


def test_token_pass_carries_the_tenant(fake_graph):
    _await(registry.get("graph_search").handler(
        {"user_id": USER, "tenant_id": TENANT_A}, query="the Agentic AI project"))
    tok = [c for c in fake_graph.calls if c[0] == "tokens"]
    assert tok and tok[0][2] == TENANT_A


def test_token_query_is_parameterised():
    from backend.knowledge_graph import queries as q
    cypher = q.tenant_search_entities_any_token()
    assert "$terms" in cypher and "$tenant" in cypher
    assert "'" not in cypher and '"' not in cypher
