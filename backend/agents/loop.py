"""The POC-3 execution loop: plan → retrieve → draft → verify → finalize.

REUSE, stated precisely, because "reuse the runtime" can mean several things:

  * LangGraph itself — same library, same StateGraph, same compile.
  * `AgentState` — extended by TypedDict inheritance, NOT copied and NOT edited.
    `AgentRunState` is `AgentState` plus this loop's fields, so the tenant,
    user, session and agent identity all arrive with the semantics the existing
    runtime already gives them.
  * `orchestrator.llm` — every model call. No agent picks a model.
  * `orchestrator.knowledge.knowledge_search` — every retrieval, and therefore
    the POC-2 tenant predicate for both stores.

What this is NOT: a second orchestration framework, a second tool registry, a
second retrieval implementation, or a second state store. It is a second GRAPH
in the existing framework — a linear pipeline, because plan→retrieve→verify is
a pipeline, while `orchestrator/graph.py` is a CYCLIC tool-calling executor.
Forcing this shape into that cycle would mean a planner re-entering the model
loop on every step, which is the uncontrolled loop the brief forbids.

THE ORDERING THAT MATTERS: `finalize` reads the verdict, not the draft. An
answer exists after `draft`, and is released only if `verify` permits it. That
edge is the whole point of POC-3 — remove it and the system answers from
retrieval alone, which is the behaviour this exists to prevent.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from backend.orchestrator import graph
from backend.orchestrator.graph import AgentState

from .contract import AgentRequest, AgentResult, AgentStatus
from .knowledge_agent import Evidence, KnowledgeAgent, render_for_prompt
from .planner import Plan, PlannerAgent
from .verification_agent import Verdict, Verification, VerificationAgent

log = logging.getLogger("aganeti.agents.loop")

DRAFT_TIER = "tool"

#: What the user is told when the evidence does not support an answer. It names
#: the gap rather than apologising, because "I don't know" is only useful if it
#: says what is missing.
_REFUSAL = ("I could not answer this from the available evidence.\n\n{why}\n\n"
            "{missing}What I searched: the corporate corpus and the knowledge graph.")

_DRAFT_SYSTEM = (
    "Answer the question using ONLY the evidence provided. Cite the markers you "
    "used ([D1], [G2], …) inline. If the evidence does not answer the question, "
    "say exactly what is missing instead of filling the gap. Never use knowledge "
    "that is not in the evidence."
)


class AgentRunState(AgentState, total=False):
    """`AgentState` plus what this loop needs to be traceable.

    Inherited, not redefined: `user_id`, `tenant_id`, `session_id`, `agent_id`
    and the rest keep the meaning the existing runtime gives them. Everything
    below is additive, and `total=False` means no existing construction site
    has to supply them.
    """

    request: str
    plan: dict
    current_step: str
    evidence: dict
    verification: dict
    final_answer: str
    trace: list
    errors: list


def _record(state: dict, agent: str, result: AgentResult) -> list:
    """One trace entry per agent execution. No payloads, no secrets — the
    evidence and verdict live in their own state fields; this is the sequence."""
    entry = {"agent": agent, "status": result.status.value,
             "took_ms": round(result.took_ms, 1), "errors": list(result.errors)}
    return list(state.get("trace") or []) + [entry]


def _req(state: dict, task: str, context: Optional[dict] = None) -> AgentRequest:
    """Build an agent request from graph state.

    The tenant is read from state and passed explicitly — the one place it could
    be dropped, so it is done once, here, rather than in each agent.
    """
    return AgentRequest(task=task, user_id=state.get("user_id") or "",
                        tenant_id=state.get("tenant_id") or "",
                        session_id=state.get("session_id") or "",
                        context=context or {})


# ── nodes ────────────────────────────────────────────────────────────────────

async def _plan_node(state: AgentRunState) -> dict:
    agent = PlannerAgent()
    result = await agent.run(_req(state, state.get("request") or ""))
    plan: Plan = result.output
    return {"plan": plan.as_dict(), "current_step": "plan",
            "trace": _record(state, "planner", result),
            "errors": list(state.get("errors") or []) + list(result.errors)}


async def _knowledge_node(state: AgentRunState) -> dict:
    """Execute the plan's knowledge step. Falls back to the user's question when
    the plan named no retrieval task — retrieval is not optional in this loop."""
    steps = (state.get("plan") or {}).get("steps") or []
    task = next((s.get("task") for s in steps if s.get("agent") == "knowledge"), None)
    result = await KnowledgeAgent().run(_req(state, task or state.get("request") or ""))
    evidence: Evidence = result.output if result.output is not None else Evidence(query="")
    return {"evidence": evidence.as_dict(), "current_step": "knowledge",
            "trace": _record(state, "knowledge", result),
            "errors": list(state.get("errors") or []) + list(result.errors)}


async def _draft_node(state: AgentRunState) -> dict:
    """Draft an answer FROM THE EVIDENCE. Never from the model's own knowledge.

    Skipped entirely when there is no evidence: asking a model to answer with an
    empty evidence block is the exact invitation to hallucinate that this loop
    exists to remove.
    """
    from backend.orchestrator import llm

    ev = _evidence_from(state)
    if not ev.citations:
        return {"current_step": "draft",
                "trace": list(state.get("trace") or []) + [
                    {"agent": "draft", "status": AgentStatus.SKIPPED.value,
                     "took_ms": 0.0, "errors": ["no evidence to draft from"]}]}

    started = time.perf_counter()
    errors: list[str] = []
    draft = ""
    try:
        msg = await llm.chat(
            [{"role": "system", "content": _DRAFT_SYSTEM},
             {"role": "user", "content": (f"QUESTION:\n{state.get('request') or ''}\n\n"
                                          f"EVIDENCE:\n{render_for_prompt(ev)}")}],
            tier=DRAFT_TIER, temperature=0.0, max_tokens=700,
            ctx={"user_id": state.get("user_id") or "", "agent_id": "draft"},
        )
        draft = (msg.get("content") or "").strip()
    except Exception as e:  # noqa: BLE001
        errors.append(f"draft: gateway unavailable ({type(e).__name__})")

    return {"final_answer": draft, "current_step": "draft",
            "trace": list(state.get("trace") or []) + [
                {"agent": "draft",
                 "status": (AgentStatus.OK if draft else AgentStatus.FAILED).value,
                 "took_ms": round((time.perf_counter() - started) * 1000, 1),
                 "errors": errors}],
            "errors": list(state.get("errors") or []) + errors}


async def _verify_node(state: AgentRunState) -> dict:
    ev = _evidence_from(state)
    result = await VerificationAgent().run(_req(
        state, "verify the drafted answer",
        {"evidence": ev, "draft": state.get("final_answer") or "",
         "question": state.get("request") or ""}))
    verification: Verification = result.output
    return {"verification": verification.as_dict(), "current_step": "verify",
            "trace": _record(state, "verification", result),
            "errors": list(state.get("errors") or []) + list(result.errors)}


async def _finalize_node(state: AgentRunState) -> dict:
    """Release the draft, or replace it with an honest refusal.

    Reads the VERDICT. A draft that exists but was not supported is discarded —
    that is the difference between a system that retrieves and one that answers.
    """
    v = state.get("verification") or {}
    verdict = str(v.get("verdict") or Verdict.UNSUPPORTED.value)
    draft = (state.get("final_answer") or "").strip()

    if v.get("releasable") and draft:
        answer = draft
        if verdict == Verdict.PARTIALLY_SUPPORTED.value:
            gaps = "; ".join(v.get("missing") or []) or "some claims are not fully evidenced"
            answer = f"{draft}\n\n(Partially supported — not established by the evidence: {gaps}.)"
    else:
        missing = v.get("missing") or []
        answer = _REFUSAL.format(
            why=v.get("explanation") or "The evidence does not support an answer.",
            missing=(f"Missing: {'; '.join(missing)}.\n\n" if missing else ""))

    return {"final_answer": answer, "current_step": "final",
            "trace": list(state.get("trace") or []) + [
                {"agent": "finalize", "status": AgentStatus.OK.value,
                 "took_ms": 0.0, "errors": [], "verdict": verdict,
                 "released": bool(v.get("releasable") and draft)}]}


def _evidence_from(state: dict) -> Evidence:
    d = state.get("evidence") or {}
    return Evidence(query=d.get("query") or "", tenant_id=d.get("tenant_id") or "",
                    citations=list(d.get("citations") or []),
                    tenant_enforced_by=list(d.get("tenant_enforced_by") or []),
                    providers_used=list(d.get("providers_used") or []))


# ── the graph ────────────────────────────────────────────────────────────────

def build_graph():
    """plan → knowledge → draft → verify → finalize. Linear and acyclic.

    No conditional edges and no loop back: every run performs exactly these five
    steps once. `draft` self-skips without evidence and `finalize` refuses on the
    verdict, so the empty-evidence path costs one skipped node rather than a
    branch that could be mis-wired.
    """
    g = StateGraph(AgentRunState)
    g.add_node("plan", _plan_node)
    g.add_node("knowledge", _knowledge_node)
    g.add_node("draft", _draft_node)
    g.add_node("verify", _verify_node)
    g.add_node("finalize", _finalize_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "knowledge")
    g.add_edge("knowledge", "draft")
    g.add_edge("draft", "verify")
    g.add_edge("verify", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()


async def run_agent_loop(request: str, *, user_id: str, tenant_id: str = "",
                         session_id: str = "", agent_id: str = "poc3") -> dict:
    """One full execution. Returns the final state — plan, evidence, verdict,
    answer and trace — so a caller can show its work without a second call."""
    # `AgentRunState` extends `AgentState`, so it needs every field the base
    # declares. Built through the one factory (§7.1) rather than as a literal:
    # this was the FOURTH construction site — one more than the audit found — and
    # it is the clearest possible case for consolidating, because a field added to
    # the base would fail here, in POC-3, for a change made somewhere else entirely.
    state: dict[str, Any] = {
        **graph.new_state(messages=[], user_id=user_id, tenant_id=tenant_id,
                          session_id=session_id, agent_id=agent_id,
                          allowed_tools=[], has_image=False),
        "request": request, "trace": [], "errors": [],
    }
    return await GRAPH.ainvoke(state)
