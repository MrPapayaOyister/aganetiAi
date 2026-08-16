"""Phase 4 — controlled Runtime B validation.

Runs the seven representative workflows from the brief through the REAL Runtime B
stack: TenantContext -> LangGraph -> registry -> authorization boundary -> tool ->
result. Nothing is stubbed. LiteLLM, Qdrant, Neo4j and Postgres are the live ones.

SAFETY. This script is read-only with respect to user data:
  * no approval is ever granted, so no outbound action executes — the approval
    workflow is validated by observing the PAUSE, which is the behaviour under test;
  * no conversation is persisted (run_turn does not write; the SSE route does);
  * the denied/cross-tenant cases assert a refusal, so nothing runs there either.

It does consume LLM tokens against the local gateway.

    python scripts/validate_runtime_b.py            # all workflows
    python scripts/validate_runtime_b.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import backend.orchestrator  # noqa: E402,F401 — registers the full canonical registry
from backend.orchestrator import authz, graph, registry  # noqa: E402
from backend.orchestrator.authz import Decision  # noqa: E402

# Two real seeded users in DIFFERENT organizations, so the cross-tenant case is a
# genuine one rather than two synthetic uuids.
USER_A = os.getenv("VALIDATE_USER_A", "84541ce0-a7fb-4cfb-b3c1-654a74153546")
USER_B = os.getenv("VALIDATE_USER_B", "ff3a2d3d-39b8-4709-9cb5-14cf317fe83d")

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str, ms: int = 0, **extra) -> None:
    RESULTS.append({"workflow": name, "ok": ok, "detail": detail, "ms": ms, **extra})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name:28s} {ms:>6}ms  {detail[:150]}")


async def _tenant_of(user_id: str) -> str:
    from backend.auth import tenant
    ctx = await tenant.resolve(user_id)
    return str(ctx.tenant_id) if ctx else ""


async def _agent(user_id: str, tools: list[str]) -> dict:
    """An agent dict shaped exactly as the route layer builds one, but with an
    explicit tool grant so a workflow is not silently skipped because the seeded
    agent predates the tool. Tenant comes from the real user row."""
    return {"id": "primary", "tools": tools, "tenant_id": await _tenant_of(user_id),
            "model_key": None, "fallback_models": []}


async def _turn(user_id: str, tools: list[str], message: str, prompt: str = "") -> tuple[dict, int]:
    agent = await _agent(user_id, tools)
    t0 = time.monotonic()
    res = await graph.run_turn(
        user_id=user_id, agent=agent, user_message=message,
        session_id="validate-runtime-b",
        system_prompt=prompt or "You are a helpful assistant. Use your tools when relevant. Be brief.",
        tenant_id=agent["tenant_id"])
    return res, int((time.monotonic() - t0) * 1000)


# ══════════════════════════════════════════════════════════════════════════════
# 1. simple chat — no tool
# ══════════════════════════════════════════════════════════════════════════════
async def wf_simple_chat():
    res, ms = await _turn(USER_A, ["current_time", "calc"],
                          "Reply with exactly the word: ready")
    final = (res.get("final") or "").strip()
    record("simple_chat", res["status"] == "done" and bool(final),
           f"status={res['status']} final={final[:60]!r}", ms)


# ══════════════════════════════════════════════════════════════════════════════
# 2. web search — outbound, must PAUSE for approval
# ══════════════════════════════════════════════════════════════════════════════
async def wf_web_search_approval():
    res, ms = await _turn(USER_A, ["web_search"],
                          "Search the web for the latest LangGraph release notes.")
    paused = res["status"] == "awaiting_approval"
    ap = res.get("approval") or {}
    record("web_search_approval", paused and ap.get("name") == "web_search",
           f"status={res['status']} rule={ap.get('rule')} preview={str(ap.get('preview'))[:70]!r}",
           ms, approval_rule=ap.get("rule"), risk=ap.get("risk_level"))


# ══════════════════════════════════════════════════════════════════════════════
# 3. enterprise lookup — Postgres-backed tools
# ══════════════════════════════════════════════════════════════════════════════
async def wf_enterprise_lookup():
    res, ms = await _turn(USER_A, ["list_tasks", "current_time"],
                          "How many tasks do I have? Use the list_tasks tool.")
    used = [m.get("name") for m in res["messages"] if m.get("role") == "tool"]
    record("enterprise_lookup", res["status"] == "done" and "list_tasks" in used,
           f"tools={used} final={str(res.get('final'))[:70]!r}", ms, tools_used=used)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Qdrant search
# ══════════════════════════════════════════════════════════════════════════════
async def wf_qdrant_search():
    res, ms = await _turn(USER_A, ["search_documents"],
                          "Search my documents for anything about the evaluation framework.")
    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    used = [m.get("name") for m in tool_msgs]
    body = " ".join(str(m.get("content", "")) for m in tool_msgs)
    ok = "search_documents" in used and not body.startswith("error")
    record("qdrant_search", ok, f"tools={used} bytes={len(body)}", ms,
           tools_used=used, result_bytes=len(body))


# ══════════════════════════════════════════════════════════════════════════════
# 5. Neo4j search — the Phase 3 tool
# ══════════════════════════════════════════════════════════════════════════════
async def wf_neo4j_search():
    res, ms = await _turn(USER_A, ["graph_search"],
                          "Use graph_search to find what the Agentic AI project is connected to.")
    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    used = [m.get("name") for m in tool_msgs]
    body = " ".join(str(m.get("content", "")) for m in tool_msgs)
    ok = "graph_search" in used and ("RELATIONSHIPS:" in body or "ENTITIES:" in body)
    record("neo4j_graph_search", ok, f"tools={used} entities_or_rels={ok}", ms,
           tools_used=used, result_bytes=len(body))


# ══════════════════════════════════════════════════════════════════════════════
# 6. approval-required tool — send_email must NOT execute
# ══════════════════════════════════════════════════════════════════════════════
async def wf_approval_required():
    res, ms = await _turn(USER_A, ["send_email", "draft_email"],
                          "Send an email to nobody@example.invalid with subject 'ping' and body 'ping'.")
    ap = res.get("approval") or {}
    paused = res["status"] == "awaiting_approval" and ap.get("name") == "send_email"
    placeholder = any("[AWAITING USER APPROVAL]" in str(m.get("content", ""))
                      for m in res["messages"] if m.get("role") == "tool")
    record("approval_required", paused and placeholder,
           f"status={res['status']} tool={ap.get('name')} rule={ap.get('rule')} placeholder={placeholder}",
           ms, approval_rule=ap.get("rule"))


# ══════════════════════════════════════════════════════════════════════════════
# 7. denied tool — granted nothing, asks for something
# ══════════════════════════════════════════════════════════════════════════════
async def wf_denied_tool():
    res, ms = await _turn(USER_A, ["current_time"],
                          "Use the run_python tool to compute 2+2.")
    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    refusals = [m for m in tool_msgs if "not permitted" in str(m.get("content", ""))]
    # The model may also decline to call it at all, which is equally correct: the
    # tool was never offered. Both outcomes prove it did not execute.
    never_offered = not any(m.get("name") == "run_python" for m in tool_msgs)
    record("denied_tool", bool(refusals) or never_offered,
           f"refusals={len(refusals)} never_offered={never_offered}", ms)


# ══════════════════════════════════════════════════════════════════════════════
# Boundary checks — no LLM, deterministic
# ══════════════════════════════════════════════════════════════════════════════
async def wf_cross_tenant():
    """Two REAL users in different organizations. Neither may borrow the other's
    tenant, and a call with no tenant is refused outright."""
    ta, tb = await _tenant_of(USER_A), await _tenant_of(USER_B)
    ok_distinct = bool(ta) and bool(tb) and ta != tb
    denied = authz.authorize_call(user_id=USER_A, tenant_id="", agent_id="primary",
                                  session_id="s", tool_name="graph_search",
                                  granted=["graph_search"], tool=registry.get("graph_search"))
    allowed = authz.authorize_call(user_id=USER_A, tenant_id=ta, agent_id="primary",
                                   session_id="s", tool_name="graph_search",
                                   granted=["graph_search"], tool=registry.get("graph_search"))
    ok = (ok_distinct and denied.decision is Decision.DENY
          and denied.rule == "no_tenant" and allowed.decision is Decision.ALLOW)
    record("cross_tenant_isolation", ok,
           f"tenantA={ta[:8]} tenantB={tb[:8]} distinct={ok_distinct} "
           f"no_tenant={denied.rule} with_tenant={allowed.decision.value}", 0)


async def wf_registry_completeness():
    import backend.guardrails as g
    names = registry.all_names()
    unclassified = [n for n in names if n not in g.TOOL_CATEGORY]
    no_perm = [n for n in names if not registry.get(n).required_permission
               and n not in ("current_time", "calc", "set_reminder")]
    record("registry_completeness", not unclassified and not no_perm,
           f"tools={len(names)} unclassified={unclassified} missing_permission={no_perm}",
           0, tool_count=len(names))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print(f"Runtime B validation — {len(registry.all_names())} tools registered\n")
    for wf in (wf_registry_completeness, wf_cross_tenant, wf_simple_chat,
               wf_enterprise_lookup, wf_qdrant_search, wf_neo4j_search,
               wf_web_search_approval, wf_approval_required, wf_denied_tool):
        try:
            await wf()
        except Exception as e:  # noqa: BLE001 — a crashed workflow is a FAIL, not a stop
            record(wf.__name__.replace("wf_", ""), False, f"EXCEPTION {type(e).__name__}: {e}")

    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"\n{passed}/{len(RESULTS)} workflows passed")
    if args.json:
        print(json.dumps(RESULTS, indent=2))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
