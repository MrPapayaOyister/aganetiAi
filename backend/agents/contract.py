"""The minimal agent contract. (POC-3)

Deliberately small. Phase 6 defines a full `AgentManifest` — declarative,
versioned, signable, loadable per tenant — and this is NOT that. It is the
smallest shape three real agents need in order to run, chosen so it can grow
into a manifest rather than be replaced by one:

    AgentSpec   is the DECLARATION  — what a manifest will eventually carry
    AgentResult is the OUTCOME      — what one execution produced
    Agent       is the CALL         — how the runtime invokes it

The split matters. `AgentSpec` holds only fields a manifest could serialise
(name, description, capabilities); nothing executable and no closures live on
it. So the eventual migration is `AgentSpec.from_manifest(...)` plus a handler
lookup — the same projection the tool registry already uses for `Tool`, where
the manifest is data and the callable is bound at load time.

What is deliberately ABSENT, so the POC does not pretend to be the platform:
no version, no pack, no entitlement, no tool allowlist, no policy reference, no
model profile. Each is a Phase-6 field. Adding them now would be inventing a
contract nobody has consumed yet, and the audit's finding was that guessed
contracts are the expensive kind.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


class AgentStatus(str, Enum):
    """How one agent execution ended.

    `INSUFFICIENT` is separate from `FAILED` on purpose: an agent that ran
    correctly and found nothing is not a broken agent, and collapsing the two
    would make "no evidence exists" indistinguishable from "retrieval was
    down" — which is exactly the distinction the verification step needs in
    order to refuse honestly rather than blame the corpus.
    """

    OK = "ok"
    INSUFFICIENT = "insufficient"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class AgentSpec:
    """The declaration. Serialisable; the Phase-6 manifest seed."""

    name: str
    description: str
    #: Coarse capability tags. Free-form at v1 BECAUSE the vocabulary is not
    #: known yet — the planner matches on `name`, not on these, so a wrong tag
    #: cannot misroute a step. When Phase 6 fixes the vocabulary, this field
    #: gains a validator instead of changing shape.
    capabilities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "capabilities": list(self.capabilities)}


@dataclass
class AgentRequest:
    """One unit of work handed to an agent.

    `tenant_id` is carried explicitly rather than read from a ContextVar. The
    ContextVar exists (backend/auth/tenant.py) and is the fallback for code far
    from the request, but an agent that can be handed its tenant should be —
    an isolation boundary that depends on ambient state is one `asyncio.to_thread`
    from being silently absent.
    """

    task: str
    user_id: str
    tenant_id: str = ""
    session_id: str = ""
    #: Everything upstream produced. Read-only from the agent's perspective.
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """The outcome of one execution."""

    agent: str
    status: AgentStatus
    #: The agent's own product — a plan, a verdict, a draft. Shape is per-agent
    #: and deliberately untyped here; the runtime does not interpret it.
    output: Any = None
    #: Provenance, carried verbatim from whatever produced it. The knowledge
    #: agent fills this from knowledge_search's citations; nothing re-derives or
    #: re-formats it, because a second provenance representation is a second
    #: thing to keep true.
    evidence: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Wall-clock, for the trace. Not a metric; a debugging aid.
    took_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status is AgentStatus.OK

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@runtime_checkable
class Agent(Protocol):
    """What the runtime requires. Three members, no base class.

    A Protocol rather than an ABC so a future plugin agent does not have to
    import this package to satisfy it — which is the property that lets an
    agent eventually arrive from a pack.
    """

    spec: AgentSpec

    async def run(self, request: AgentRequest) -> AgentResult: ...


class BaseAgent:
    """Optional convenience: timing, and failures that never escape.

    An agent raising into the graph would abort a run that could still produce
    a useful, honest partial answer ("retrieval failed, here is why"). So the
    exception becomes a FAILED result and the loop decides what to do with it.
    """

    spec: AgentSpec

    async def run(self, request: AgentRequest) -> AgentResult:
        started = time.perf_counter()
        try:
            result = await self._run(request)
        except Exception as e:  # noqa: BLE001 — an agent fault is data, not a crash
            result = AgentResult(agent=self.spec.name, status=AgentStatus.FAILED,
                                 errors=[f"{type(e).__name__}: {e}"])
        result.took_ms = (time.perf_counter() - started) * 1000
        return result

    async def _run(self, request: AgentRequest) -> AgentResult:  # pragma: no cover
        raise NotImplementedError
