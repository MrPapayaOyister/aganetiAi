"""Planner Agent — turns a request into a bounded, inspectable plan. (POC-3)

NAMING, because there are now two things called "plan" and confusing them would
be expensive: `orchestrator/router.plan()` chooses MODELS (an ordered list of
model_keys to try). This chooses STEPS. They compose — this planner's steps are
executed by agents that each call the gateway, which calls router.plan() to pick
a model. Neither knows about the other.

BOUNDED BY CONSTRUCTION, not by prompt instruction:
  * `MAX_STEPS` truncates, so a model that emits fifty steps cannot run fifty.
  * A step naming an unknown agent is DROPPED, not executed. The registry of
    legal agents is passed in; the model cannot invent a capability.
  * There is no loop and no re-planning. The plan is produced once and executed
    once, which is what "no uncontrolled autonomous loop" means here.

FALLBACK. If the model returns something unparseable, the planner does NOT fail
the run — it falls back to the deterministic knowledge→verify plan, records
`fallback_used`, and says so in its errors. This is a safety net, not the normal
path: the tests assert the model IS called and its plan IS used when it parses,
so the fallback cannot quietly become the implementation.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from .contract import AgentRequest, AgentResult, AgentSpec, AgentStatus, BaseAgent

log = logging.getLogger("aganeti.agents.planner")

#: Hard cap. A plan longer than this is truncated, never executed in full.
MAX_STEPS = 4

#: Capability/task profile, NOT a model name. The gateway resolves this to a
#: concrete model via router.plan(); changing the deployment's model map must not
#: require editing this file — which acceptance scenario 6 asserts.
PLANNER_TIER = "tool"


@dataclass
class PlanStep:
    id: str
    agent: str
    task: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    #: True when the model's output could not be used and the deterministic
    #: plan was substituted. Surfaced so a demo cannot silently show a
    #: hand-written plan as if the model had produced it.
    fallback_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "steps": [s.as_dict() for s in self.steps],
                "fallback_used": self.fallback_used}


_SYSTEM = (
    "You plan how to answer a question using a fixed set of agents. "
    "Reply with ONLY a JSON object, no prose and no code fence:\n"
    '{"goal": "<one sentence>", "steps": [{"id": "1", "agent": "<agent>", "task": "<what to do>"}]}\n'
    "Rules: use ONLY the agents listed; at most %d steps; always retrieve evidence "
    "before verifying it; never invent an agent."
)


def default_plan(question: str) -> Plan:
    """The deterministic plan: retrieve, then verify.

    Also the fallback. It is the shape POC-3 exists to demonstrate, so a model
    that agrees with it has not been bypassed — it has agreed.
    """
    return Plan(
        goal=f"Answer the question using corporate knowledge: {question}".strip(),
        steps=[
            PlanStep(id="1", agent="knowledge",
                     task=f"Find evidence that answers: {question}"),
            PlanStep(id="2", agent="verification",
                     task="Verify the drafted answer is supported by the evidence"),
        ],
    )


def _parse(raw: str, allowed: Sequence[str], question: str) -> Optional[Plan]:
    """Parse the model's plan, dropping anything not permitted.

    Returns None when nothing usable survives, which is the signal to fall back.
    """
    text = (raw or "").strip()
    if text.startswith("```"):                      # tolerate a fenced block
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        text = text.removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    steps: list[PlanStep] = []
    for raw_step in (obj.get("steps") or [])[:MAX_STEPS]:
        if not isinstance(raw_step, dict):
            continue
        agent = str(raw_step.get("agent", "")).strip().lower()
        task = str(raw_step.get("task", "")).strip()
        if agent not in allowed or not task:
            # An unknown agent is DROPPED rather than executed. This is the line
            # that stops a model naming `shell` or `enterprise_action` and having
            # the runtime try to honour it.
            log.info("planner: dropped step for unknown/empty agent %r", agent)
            continue
        steps.append(PlanStep(id=str(raw_step.get("id") or len(steps) + 1),
                              agent=agent, task=task))
    if not steps:
        return None
    goal = str(obj.get("goal") or "").strip() or f"Answer: {question}"
    return Plan(goal=goal, steps=steps)


class PlannerAgent(BaseAgent):
    spec = AgentSpec(
        name="planner",
        description="Decomposes a user request into ordered steps for the available agents.",
        capabilities=("planning", "decomposition"),
    )

    def __init__(self, allowed_agents: Sequence[str] = ("knowledge", "verification")):
        self.allowed_agents = tuple(a.lower() for a in allowed_agents)

    async def _run(self, request: AgentRequest) -> AgentResult:
        from backend.orchestrator import llm

        question = request.task
        errors: list[str] = []
        plan: Optional[Plan] = None

        try:
            msg = await llm.chat(
                [{"role": "system", "content": _SYSTEM % MAX_STEPS},
                 {"role": "user",
                  "content": f"Agents: {', '.join(self.allowed_agents)}\n"
                             f"Question: {question}"}],
                # tier, not a model name — the gateway owns model choice.
                tier=PLANNER_TIER, temperature=0.0, max_tokens=400,
                ctx={"user_id": request.user_id, "agent_id": self.spec.name},
            )
            plan = _parse(msg.get("content") or "", self.allowed_agents, question)
            if plan is None:
                errors.append("planner: model output unusable; used the deterministic plan")
        except Exception as e:  # noqa: BLE001 — a gateway outage must not end the run
            errors.append(f"planner: gateway unavailable ({type(e).__name__}); "
                          f"used the deterministic plan")

        if plan is None:
            plan = default_plan(question)
            plan.fallback_used = True

        return AgentResult(agent=self.spec.name, status=AgentStatus.OK,
                           output=plan, errors=errors)
