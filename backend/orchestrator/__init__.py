"""Enterprise Agentic OS — agent orchestrator.

Real LangGraph tool-calling executor with:
  - dynamic tool-calling (agent ⇄ tools cyclic loop),
  - delegation-as-a-tool (primary → specialist nested runs — the mesh),
  - a hard OUTBOUND approval gate (pause → approve/reject → resume),
  - streaming (SSE) for the chat UI.

Importing this package registers all v1 tools + the delegate tool.
Public entrypoints: run_turn(), resume(), astream_turn().
"""
from . import router, llm, registry, graph, agents, store  # noqa: F401  (agents registers `delegate`)
from .graph import run_turn, resume, astream_turn, GRAPH, AgentState
from .registry import Tool, register, get, openai_schemas, ApprovalRequired
from .agents import SPECIALISTS, specialist_names
from .router import MODELS, plan

__all__ = [
    "router", "llm", "registry", "graph", "agents", "store",
    "run_turn", "resume", "astream_turn", "GRAPH", "AgentState",
    "Tool", "register", "get", "openai_schemas", "ApprovalRequired",
    "SPECIALISTS", "specialist_names", "MODELS", "plan",
]
