"""Verification Agent — decides whether the evidence supports the answer. (POC-3)

The point of POC-3 is that an answer is not released because retrieval returned
something. This is the step that can refuse.

DETERMINISTIC FLOOR, THEN JUDGEMENT. Two verdicts are decided in code and never
asked of a model:

  * no evidence at all            -> UNSUPPORTED
  * a draft that already declined -> UNSUPPORTED

Both are cases where a model could be talked into agreeing that nothing supports
something, and where being wrong means fabricating. Everything above that floor
is a judgement call and does go to the gateway — that is the part a rule cannot
do well.

The model's verdict is then CLAMPED: it may not return SUPPORTED when there is
no evidence. A verifier that can be argued out of its own precondition is not a
verifier.

Not an elaborate framework, deliberately. No entailment scoring, no per-claim
alignment, no confidence calibration. Four verdicts, four fields.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from .contract import AgentRequest, AgentResult, AgentSpec, AgentStatus, BaseAgent
from .knowledge_agent import Evidence, render_for_prompt

log = logging.getLogger("aganeti.agents.verification")

#: Capability profile, not a model name — same rule as the planner.
VERIFIER_TIER = "tool"


class Verdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTING = "CONFLICTING"


#: Verdicts that permit releasing the drafted answer. PARTIALLY_SUPPORTED does —
#: with its caveat attached — because refusing a partly-evidenced answer outright
#: is its own kind of dishonesty when the evidence genuinely covers some of it.
RELEASABLE = {Verdict.SUPPORTED, Verdict.PARTIALLY_SUPPORTED}


@dataclass
class Verification:
    verdict: Verdict
    explanation: str = ""
    supporting: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    #: True when a rule decided this, not the model.
    deterministic: bool = False

    @property
    def releasable(self) -> bool:
        return self.verdict in RELEASABLE

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        d["releasable"] = self.releasable
        return d


_SYSTEM = (
    "You check whether cited evidence supports a proposed answer. You are not "
    "answering the question yourself.\n"
    "Reply with ONLY a JSON object, no prose and no code fence:\n"
    '{"verdict": "SUPPORTED|PARTIALLY_SUPPORTED|UNSUPPORTED|CONFLICTING", '
    '"explanation": "<one or two sentences>", "supporting": ["<marker or quote>"], '
    '"missing": ["<what the evidence does not establish>"], '
    '"conflicts": ["<statements that disagree, if any>"]}\n'
    "Use CONFLICTING when two pieces of evidence disagree with each other. "
    "Use UNSUPPORTED when the evidence does not establish the answer. "
    "Judge ONLY against the evidence shown; your own knowledge is not evidence."
)


#: Phrases a draft uses when it is declining rather than answering. Matched on a
#: NORMALISED copy, and only against the draft — never against evidence text,
#: which may legitimately discuss what a policy does not cover.
_DECLINE_PATTERNS = (
    "does not mention", "does not contain", "does not provide", "does not specify",
    "does not address", "does not include", "does not answer", "does not cover",
    "no information about", "no information on", "not mentioned in the evidence",
    "the evidence does not", "cannot be determined", "not possible to determine",
    "unable to determine", "i could not answer", "i cannot answer",
    "insufficient evidence", "no relevant evidence",
)


def _draft_declines(draft: str) -> bool:
    """Is this draft a refusal rather than an answer?

    Deliberately a phrase list, not a model call. Asking a model "did you just
    decline?" reintroduces the same judgement that produced the wrong verdict in
    the first place, and this decision has to be the reliable one.

    False positives are the safe direction: mislabelling a real answer as
    unsupported withholds it, which is recoverable; mislabelling a refusal as
    supported puts a green badge on "I don't know", which is not.
    """
    t = " ".join((draft or "").lower().split())
    return any(p in t for p in _DECLINE_PATTERNS)


def _parse(raw: str) -> Optional[Verification]:
    text = (raw or "").strip()
    if text.startswith("```"):
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
    try:
        verdict = Verdict(str(obj.get("verdict", "")).strip().upper())
    except ValueError:
        return None

    def _strs(key: str) -> list[str]:
        v = obj.get(key)
        return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []

    return Verification(verdict=verdict,
                        explanation=str(obj.get("explanation") or "").strip(),
                        supporting=_strs("supporting"), missing=_strs("missing"),
                        conflicts=_strs("conflicts"))


class VerificationAgent(BaseAgent):
    spec = AgentSpec(
        name="verification",
        description="Judges whether retrieved evidence supports a proposed answer.",
        capabilities=("verification", "grounding"),
    )

    async def _run(self, request: AgentRequest) -> AgentResult:
        from backend.orchestrator import llm

        evidence: Optional[Evidence] = request.context.get("evidence")
        draft: str = (request.context.get("draft") or "").strip()
        question: str = request.context.get("question") or request.task

        # ── deterministic floor ──────────────────────────────────────────────
        if evidence is None or not evidence.citations:
            v = Verification(
                verdict=Verdict.UNSUPPORTED, deterministic=True,
                explanation="No evidence was retrieved, so nothing supports an answer.",
                missing=["any corpus or graph evidence for this question"])
            return AgentResult(agent=self.spec.name, status=AgentStatus.OK, output=v)

        if not draft:
            v = Verification(
                verdict=Verdict.UNSUPPORTED, deterministic=True,
                explanation="No answer was drafted from the evidence.",
                missing=["a drafted answer to check"])
            return AgentResult(agent=self.spec.name, status=AgentStatus.OK, output=v)

        if _draft_declines(draft):
            # A draft that says "the evidence does not cover this" is TRIVIALLY
            # supported by the evidence — the model is asked "is this answer
            # supported?", and the absence of evidence does support the claim
            # that evidence is absent. Live validation produced exactly that:
            # SUPPORTED on a refusal, which a UI renders as a green badge over
            # "I don't know". The question the verdict must answer is whether
            # the QUESTION was answered, so a declining draft is UNSUPPORTED by
            # rule and the model is not consulted.
            v = Verification(
                verdict=Verdict.UNSUPPORTED, deterministic=True,
                explanation="The retrieved evidence does not answer the question.",
                missing=["evidence that addresses the question asked"])
            return AgentResult(agent=self.spec.name, status=AgentStatus.OK, output=v)

        # ── judgement ────────────────────────────────────────────────────────
        errors: list[str] = []
        parsed: Optional[Verification] = None
        try:
            msg = await llm.chat(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user",
                  "content": (f"QUESTION:\n{question}\n\nEVIDENCE:\n"
                              f"{render_for_prompt(evidence)}\n\n"
                              f"PROPOSED ANSWER:\n{draft}")}],
                tier=VERIFIER_TIER, temperature=0.0, max_tokens=500,
                ctx={"user_id": request.user_id, "agent_id": self.spec.name},
            )
            parsed = _parse(msg.get("content") or "")
            if parsed is None:
                errors.append("verification: model output unusable")
        except Exception as e:  # noqa: BLE001
            errors.append(f"verification: gateway unavailable ({type(e).__name__})")

        if parsed is None:
            # Fail CLOSED. An unavailable verifier must not become a pass — the
            # whole step exists to be the thing that can say no.
            v = Verification(
                verdict=Verdict.UNSUPPORTED, deterministic=True,
                explanation="The answer could not be verified, so it is not released.",
                missing=["a completed verification check"])
            return AgentResult(agent=self.spec.name, status=AgentStatus.OK,
                               output=v, errors=errors)

        # Clamp: SUPPORTED requires evidence to exist. Reached only if the model
        # contradicts its own input, but that is precisely when a guard earns its
        # keep.
        if parsed.verdict is Verdict.SUPPORTED and not evidence.citations:
            parsed.verdict = Verdict.UNSUPPORTED
            parsed.deterministic = True

        return AgentResult(agent=self.spec.name, status=AgentStatus.OK,
                           output=parsed, errors=errors)
