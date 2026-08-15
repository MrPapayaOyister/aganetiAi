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
from . import authz  # noqa: F401  (the tool authorization boundary)
from . import knowledge  # noqa: F401  (registers `knowledge_search` — the GraphRAG tool)
from . import browser_registration, consumer_tools, graph_tools  # noqa: F401
from .graph import run_turn, resume, astream_turn, GRAPH, AgentState
from .registry import Tool, register, get, openai_schemas, ApprovalRequired
from .agents import SPECIALISTS, specialist_names
from .router import MODELS, plan

# Runtime B's registry is the CANONICAL registry, so importing this package must
# yield the WHOLE catalogue — not "the v1 tools, plus whatever else the importing
# process happened to touch". Registering here rather than at some route's import
# time is what stops a repeat of `list_metrics`/`run_metric`, which existed in the
# registry of a process that had imported the dashboard router and nowhere else.
consumer_tools.register_consumer_tools()
graph_tools.register_graph_tools()
# The 14 browser tools (Phase D). Registered here for the same reason as the rest:
# the canonical registry must be complete in every process, not "whatever this one
# imported". browser_tools/ itself registers nothing and knows nothing about the
# registry — this is the only place the two meet.
browser_registration.register_browser_tools()

__all__ = [
    "router", "llm", "registry", "graph", "agents", "store", "knowledge", "authz",
    "consumer_tools", "graph_tools", "browser_registration",
    "run_turn", "resume", "astream_turn", "GRAPH", "AgentState",
    "Tool", "register", "get", "openai_schemas", "ApprovalRequired",
    "SPECIALISTS", "specialist_names", "MODELS", "plan",
]
