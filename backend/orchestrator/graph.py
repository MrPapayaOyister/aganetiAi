"""The real agent executor — a cyclic LangGraph tool-calling loop.

Replaces the linear manager→developer / triage→drafter "toy" graphs. The loop is
the standard ReAct shape but production-shaped:

    START → agent → (tool_calls? → tools → agent)*  → END

- `agent`  : one LLM turn with the agent's *allowed* tool schemas; may emit tool_calls.
- `tools`  : executes each tool call under a permission check; outbound tools raise
             ApprovalRequired (→ approval gate, wired next increment).
- routing  : conditional edge; hard STEP_BUDGET stops runaways.

Async throughout (no sync blocking in the request path). MemorySaver checkpointer
for now; swaps to the Postgres checkpointer in Phase A so approval-interrupts
survive process restarts.
"""
from __future__ import annotations

import json
import operator
from typing import Annotated, Literal, TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from . import llm, registry

STEP_BUDGET = 8          # max agent turns per run (runaway guard)
MAX_TOOL_OUTPUT = 6000   # chars fed back per tool result


class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    user_id: str
    agent_id: str
    allowed_tools: list
    step: int


async def _agent_node(state: AgentState) -> dict:
    schemas = registry.openai_schemas(state["allowed_tools"])
    msg = await llm.chat(state["messages"], tools=schemas)
    return {"messages": [msg], "step": state["step"] + 1}


async def _tools_node(state: AgentState) -> dict:
    last = state["messages"][-1]
    ctx = {"user_id": state["user_id"], "agent_id": state["agent_id"]}
    outs: list[dict] = []
    for tc in last.get("tool_calls", []):
        name = tc["function"]["name"]
        call_id = tc["id"]
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except Exception:
            args = {}
        tool = registry.get(name)
        if tool is None:
            content = f"error: unknown tool '{name}'"
        elif name not in state["allowed_tools"]:
            content = f"error: this agent is not permitted to use '{name}'"
        else:
            try:
                content = await tool.handler(ctx, **args)
            except registry.ApprovalRequired as ar:
                # Outbound action — do NOT execute. Surfaced to the approval gate
                # (next increment writes an `approvals` row + interrupts the graph).
                content = f"[awaiting approval] {ar.action_type}: {ar.preview}"
            except TypeError as e:
                content = f"error: bad arguments for {name}: {e}"
            except Exception as e:  # noqa: BLE001
                content = f"error: {name} failed: {e}"
        outs.append({"role": "tool", "tool_call_id": call_id, "name": name,
                     "content": str(content)[:MAX_TOOL_OUTPUT]})
    return {"messages": outs}


def _route(state: AgentState) -> Literal["tools", "end"]:
    if state["step"] >= STEP_BUDGET:
        return "end"
    if state["messages"] and state["messages"][-1].get("tool_calls"):
        return "tools"
    return "end"


def _build():
    g = StateGraph(AgentState)
    g.add_node("agent", _agent_node)
    g.add_node("tools", _tools_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", _route, {"tools": "tools", "end": END})
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=MemorySaver())


GRAPH = _build()


async def run(*, user_id: str, agent: dict, user_message: str, session_id: str,
              system_prompt: str | None = None) -> dict:
    """Run one user turn to completion. Returns
    {final: str, tool_calls: [names], steps: int, trace: [messages]}."""
    msgs: list[dict] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    msgs.append({"role": "user", "content": user_message})

    init: AgentState = {
        "messages": msgs,
        "user_id": user_id,
        "agent_id": agent.get("id", "primary"),
        "allowed_tools": agent.get("tools", registry.all_names()),
        "step": 0,
    }
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 2 * STEP_BUDGET + 2}
    result = await GRAPH.ainvoke(init, config)
    final = result["messages"][-1].get("content", "")
    used = [m["name"] for m in result["messages"] if m.get("role") == "tool"]
    return {"final": final, "tool_calls": used, "steps": result["step"], "trace": result["messages"]}
