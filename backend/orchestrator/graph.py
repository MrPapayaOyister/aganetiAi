"""The real agent executor — a cyclic LangGraph tool-calling loop with a hard
outbound-approval gate and delegation support.

    START → agent → (tool_calls? → tools → agent)*  → END
                                     │
                          outbound tool? → pause (awaiting approval) → END

State == the message list, so a paused run is fully captured by its messages +
the `awaiting` record (persisted by the caller in `agent_runs`). Resuming is just
re-invoking with the approved tool result appended — no checkpointer gymnastics,
survives process restarts. Async throughout.
"""
from __future__ import annotations

import json
import logging
import operator
from typing import Annotated, AsyncIterator, Literal, TypedDict

from langgraph.graph import START, END, StateGraph

from . import authz, llm, registry

log = logging.getLogger("aganeti.orchestrator.graph")

STEP_BUDGET = 8
MAX_TOOL_OUTPUT = 6000


class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    user_id: str
    tenant_id: str          # organizations.id — the isolation boundary (may be "" pre-resolution)
    session_id: str
    agent_id: str
    board_id: str
    allowed_tools: list
    step: int
    awaiting: dict | None
    model_key: str | None
    fallback_models: list
    has_image: bool


def _has_image(messages: list) -> bool:
    return any(isinstance(m.get("content"), list) for m in messages)


async def _agent_node(state: AgentState) -> dict:
    has_image = state.get("has_image", False)
    schemas = registry.openai_schemas(state["allowed_tools"])
    agent_cfg = {"model_key": state.get("model_key"), "fallback_models": state.get("fallback_models") or []}
    ctx = {"user_id": state["user_id"], "agent_id": state["agent_id"], "board_id": state.get("board_id", "")}
    # need_vision is gated at the TURN level (state.has_image), not per-message — else
    # after a tool call this node re-runs and would flip back to a text-only model.
    # On a vision turn we send NO tools: the deployed vision-vl vLLM is launched
    # without --enable-auto-tool-choice, so a tool_choice='auto' request 400s. Vision
    # turns are perception ("what's in this image?"); tool-use is a follow-up turn
    # (has_image false → normal tool model + schemas). To enable tools+vision in one
    # turn, relaunch vision-vl with --enable-auto-tool-choice --tool-call-parser hermes.
    # Analytics/chart agents write SQL — pin temperature to 0 for deterministic,
    # reproducible queries; the conversational Assistant stays at the 0.2 default.
    _temp = 0.0 if state.get("agent_id") in ("dashboard", "analytics") else 0.2
    _tc = "required" if (state.get("agent_id") == "dashboard" and state.get("step", 0) == 0 and not has_image) else "auto"
    # The analytics agent answers breakdowns/rankings as markdown TABLES. The 1024-token
    # default truncates a wide table mid-row (the row then renders as broken pipes), so
    # give the data agents room. Same blast radius as the temperature rule above.
    _maxtok = 4096 if state.get("agent_id") in ("dashboard", "analytics") else 1024
    msg = await llm.chat(state["messages"], tools=(None if has_image else schemas),
                         agent=agent_cfg, ctx=ctx, need_vision=has_image, temperature=_temp,
                         tool_choice=_tc, max_tokens=_maxtok)
    return {"messages": [msg], "step": state["step"] + 1}


async def _tools_node(state: AgentState) -> dict:
    """Execute this step's tool calls — through the authorization boundary, always.

    There is exactly ONE decision point here: `authz.authorize_call`. The inline
    unknown-tool / allowlist / is_outbound checks that used to live in this function
    are gone; re-introducing any of them would create a second policy path, which is
    how `web_search` came to be gated on one runtime and not the other. Every branch
    below is a consequence of the returned Decision, never an independent judgement.
    """
    last = state["messages"][-1]
    ctx = {"user_id": state["user_id"], "agent_id": state["agent_id"],
           "board_id": state.get("board_id", ""), "tenant_id": state.get("tenant_id", ""),
           # Carried so a handler can record WHICH conversation produced a side
           # effect. AgentState has had it since P0; the ctx did not, so a tool
           # needing provenance had to fall back to board_id (Audit Item 10).
           "session_id": state.get("session_id", "")}
    outs: list[dict] = []
    pending: list[dict] = []
    for tc in last.get("tool_calls", []):
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except Exception:
            args = {}
        tool = registry.get(name)
        verdict = authz.authorize_call(
            user_id=state["user_id"], tenant_id=state.get("tenant_id", ""),
            agent_id=state["agent_id"], session_id=state.get("session_id", ""),
            tool_name=name, arguments=args,
            granted=state["allowed_tools"], tool=tool)
        _audit(verdict)

        if verdict.denied:
            # The refusal is returned AS THE TOOL RESULT, not raised: the model must
            # see why it was refused so it can answer without the tool, and the
            # tool-call protocol requires every call to be answered.
            content = f"error: {verdict.reason}"
        elif verdict.needs_approval:
            # HARD GATE: never execute here. Record for approval; answer the call
            # with a placeholder so the tool-call protocol stays valid.
            prev = registry.preview(name, args)
            pending.append({"tool_call_id": tc["id"], "name": name, "args": args,
                            "action_type": name, "preview": prev,
                            "rule": verdict.rule, "risk_level": verdict.risk_level.value})
            content = f"[AWAITING USER APPROVAL] {prev}"
        else:
            try:
                content = await tool.handler(ctx, **args)
            except TypeError as e:
                content = f"error: bad arguments for {name}: {e}"
            except Exception as e:  # noqa: BLE001
                content = f"error: {name} failed: {e}"
        outs.append({"role": "tool", "tool_call_id": tc["id"], "name": name,
                     "content": str(content)[:MAX_TOOL_OUTPUT]})
    return {"messages": outs, "awaiting": pending[0] if pending else None}


def _audit(verdict) -> None:
    """Record every non-ALLOW decision on the events spine. Best-effort: an audit
    failure must never change whether a tool runs."""
    if verdict.allowed:
        return
    try:
        from backend import events
        req = verdict.request
        events.log_event("authz_decision", user_id=req.user_id, name=req.tool_name,
                         success=False, meta=verdict.as_dict())
    except Exception:  # noqa: BLE001
        log.debug("authz audit failed", exc_info=True)


def _route_agent(state: AgentState) -> Literal["tools", "end"]:
    if state["step"] >= STEP_BUDGET:
        return "end"
    return "tools" if (state["messages"] and state["messages"][-1].get("tool_calls")) else "end"


def _route_tools(state: AgentState) -> Literal["agent", "end"]:
    return "end" if state.get("awaiting") else "agent"


def _build():
    g = StateGraph(AgentState)
    g.add_node("agent", _agent_node)
    g.add_node("tools", _tools_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", _route_agent, {"tools": "tools", "end": END})
    g.add_conditional_edges("tools", _route_tools, {"agent": "agent", "end": END})
    return g.compile()


GRAPH = _build()


def _init(user_id: str, agent: dict, messages: list, step: int = 0, *,
          tenant_id: str = "", session_id: str = "") -> AgentState:
    """Build the executor state.

    `tenant_id` comes from the agent dict when the caller resolved one (the route
    layer does), else from the explicit argument. It is NOT defaulted to anything
    permissive: an empty tenant reaches the boundary as empty and, under
    AUTHZ_STRICT_TENANT, is refused there. That is deliberate — the failure mode of
    a forgotten tenant must be "denied", not "unscoped".
    """
    return {"messages": messages, "user_id": user_id,
            "tenant_id": str(tenant_id or agent.get("tenant_id") or ""),
            "session_id": str(session_id or ""),
            "agent_id": agent.get("id", "primary"),
            "allowed_tools": agent.get("tools", registry.all_names()), "step": step, "awaiting": None,
            "model_key": agent.get("model_key"), "fallback_models": agent.get("fallback_models") or [],
            "has_image": _has_image(messages)}


def _user_msg(user_message: str, images: list | None) -> dict:
    """A multimodal user message when images (data: URLs) are attached, else plain text."""
    if images:
        content = [{"type": "text", "text": user_message}]
        content += [{"type": "image_url", "image_url": {"url": u}} for u in images if u]
        return {"role": "user", "content": content}
    return {"role": "user", "content": user_message}


def _result(state: dict) -> dict:
    awaiting = state.get("awaiting")
    if awaiting:
        return {"status": "awaiting_approval", "approval": awaiting,
                "final": None, "messages": state["messages"], "steps": state.get("step", 0)}
    return {"status": "done", "final": state["messages"][-1].get("content", ""),
            "approval": None, "messages": state["messages"], "steps": state.get("step", 0)}


async def run_turn(*, user_id: str, agent: dict, user_message: str,
                   session_id: str = "sess", system_prompt: str | None = None,
                   history: list | None = None, images: list | None = None,
                   tenant_id: str = "") -> dict:
    msgs: list[dict] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    if history:
        msgs.extend(history)
    msgs.append(_user_msg(user_message, images))
    cfg = {"recursion_limit": 3 * STEP_BUDGET}
    out = await GRAPH.ainvoke(
        _init(user_id, agent, msgs, tenant_id=tenant_id, session_id=session_id), cfg)
    return _result(out)


async def resume(*, user_id: str, agent: dict, messages: list, approval: dict,
                 approved: bool, step: int = 0, tenant_id: str = "",
                 session_id: str = "") -> dict:
    """Continue a paused run after the user decides on an outbound action.

    The approved handler is RE-AUTHORIZED before it runs. An approval is durable —
    it can sit in the queue across a permission revocation, an agent edit, or a
    kill-switch being thrown — so the grant that existed when the run paused is not
    evidence that it still exists now. Re-deciding here is what keeps "every tool
    invocation passes the boundary" true rather than approximately true.

    The decision must come back APPROVAL_REQUIRED: that is the state a human just
    signed off on. ALLOW would mean the tool stopped being approval-gated (policy
    changed under the approval) and DENY means the grant is gone; neither is a
    mandate to execute what the user approved, so both refuse.
    """
    agent_id = agent.get("id", "primary")
    ctx = {"user_id": user_id, "agent_id": agent_id,
           "tenant_id": str(tenant_id or agent.get("tenant_id") or "")}
    if approved:
        name = approval["name"]
        tool = registry.get(name)
        verdict = authz.authorize_call(
            user_id=user_id, tenant_id=ctx["tenant_id"], agent_id=agent_id,
            session_id=session_id, tool_name=name, arguments=approval.get("args") or {},
            granted=agent.get("tools") or [], tool=tool)
        if not verdict.needs_approval:
            _audit(verdict)
            result = (f"error: {name} is no longer permitted for this agent "
                      f"({verdict.rule}); the approval was not executed.")
        else:
            try:
                result = await tool.handler(ctx, **approval["args"])
            except Exception as e:  # noqa: BLE001
                result = f"error: {name} failed after approval: {e}"
    else:
        result = f"User REJECTED this action: {approval['preview']}. Do not retry it; continue without it."
    # Replace the placeholder tool message for the approved call with the outcome.
    msgs = [dict(m) for m in messages]
    for m in msgs:
        if m.get("role") == "tool" and m.get("tool_call_id") == approval["tool_call_id"]:
            m["content"] = result
            break
    cfg = {"recursion_limit": 3 * STEP_BUDGET}
    out = await GRAPH.ainvoke(
        _init(user_id, agent, msgs, step=step, tenant_id=ctx["tenant_id"],
              session_id=session_id), cfg)
    return _result(out)


async def astream_turn(*, user_id: str, agent: dict, user_message: str,
                       session_id: str = "sess", system_prompt: str | None = None,
                       history: list | None = None, images: list | None = None,
                       tenant_id: str = "") -> AsyncIterator[dict]:
    """Stream graph events for SSE: {type: thinking|tool_call|token|approval_required|final|done}.
    LangGraph streams node updates; we translate them into UI events."""
    msgs: list[dict] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    if history:
        msgs.extend(history)
    msgs.append(_user_msg(user_message, images))
    state = _init(user_id, agent, msgs, tenant_id=tenant_id, session_id=session_id)
    acc_msgs = list(msgs)
    awaiting = None
    step = 0
    async for mode, chunk in GRAPH.astream(state, {"recursion_limit": 3 * STEP_BUDGET},
                                           stream_mode=["updates"]):
        for node, upd in chunk.items():
            if not upd:
                continue
            new_msgs = upd.get("messages", [])
            acc_msgs = acc_msgs + new_msgs
            if "step" in upd:
                step = upd["step"]
            if "awaiting" in upd:
                awaiting = upd["awaiting"]
            for m in new_msgs:
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        yield {"type": "tool_call", "name": tc["function"]["name"]}
                    if m.get("content"):
                        yield {"type": "token", "content": m["content"]}
                elif m.get("role") == "assistant":
                    yield {"type": "token", "content": m.get("content", "")}
            if awaiting:
                yield {"type": "approval_required", "approval": awaiting}
    res = _result({"messages": acc_msgs, "awaiting": awaiting, "step": step})
    yield {"type": "final", **res}
