"""POC-3 — the minimal real agent execution loop.

    request → plan → knowledge → draft → verify → finalize → answer

Three agents (planner, knowledge, verification) over the EXISTING platform:
LangGraph, `AgentState`, the model gateway, and `knowledge_search` (which is
where POC-2's tenant predicate lives for both Qdrant and Neo4j).

Importing this package registers nothing and starts nothing. `run_agent_loop`
is the entry point; see docs/architecture/poc3_execution_flow.md.
"""
from .contract import (Agent, AgentRequest, AgentResult, AgentSpec, AgentStatus,
                       BaseAgent)
from .knowledge_agent import Evidence, KnowledgeAgent
from .loop import GRAPH, AgentRunState, build_graph, run_agent_loop
from .planner import Plan, PlannerAgent, PlanStep, default_plan
from .verification_agent import Verdict, Verification, VerificationAgent

#: The agents this POC ships. Extensible by design — a fourth agent is a new
#: entry here plus a node, not a change to the contract.
AGENTS = {
    "planner": PlannerAgent,
    "knowledge": KnowledgeAgent,
    "verification": VerificationAgent,
}

__all__ = [
    "Agent", "AgentRequest", "AgentResult", "AgentSpec", "AgentStatus", "BaseAgent",
    "PlannerAgent", "Plan", "PlanStep", "default_plan",
    "KnowledgeAgent", "Evidence",
    "VerificationAgent", "Verification", "Verdict",
    "AgentRunState", "GRAPH", "build_graph", "run_agent_loop", "AGENTS",
]
