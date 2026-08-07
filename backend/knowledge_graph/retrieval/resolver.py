"""
EntityResolver — a natural-language question in, canonical graph entities out.

The LLM is used for ONE thing only: spotting which spans of the question are
entity mentions. It never decides what those mentions refer to. Resolution is
done by the registry against Neo4j and the alias table, because a model asked to
"pick the right node" will invent a plausible id, and a fabricated entity id is
indistinguishable from a real one downstream.

    question ──► LLM (mentions only) ──► registry ──► Neo4j / alias table
                                                 └──► ResolvedEntity[]

No prompt for answering is built here, and nothing is generated.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from backend.services import llm as _llm

from ..extractor import _parse_json_object
from ..normalizer import Normalizer
from ..service import GraphService
from .registry import CanonicalEntityRegistry
from .types import ResolvedEntity

log = logging.getLogger("aganeti.kg.resolver")

MENTION_TIMEOUT = 45.0
MAX_QUESTION_CHARS = 4000


def mention_prompt(question: str) -> str:
    """Ask only for the spans, not for types and not for meaning.

    Types are deliberately excluded: the graph already knows what a thing is, and
    a model guessing "Technology" for something stored as a Project would only
    add a wrong signal to resolution."""
    return f"""Identify the named things a question is asking about.

Return ONLY a JSON object:
{{"mentions": ["<exact span from the question>", ...]}}

Rules:
- Copy each mention EXACTLY as it appears in the question — do not expand
  abbreviations, do not correct spelling, do not translate.
- Include proper names of people, products, projects, systems, organisations,
  models, documents and places.
- Keep multi-word names whole: "Microsoft Graph" is ONE mention, not two.
- Do NOT include question words, verbs, dates, or generic nouns
  ("the system", "our database", "last week").
- If the question names nothing, return {{"mentions": []}}.

QUESTION:
{question}"""


class EntityResolver:
    """Question → canonical entities, via the registry."""

    def __init__(self, registry: Optional[CanonicalEntityRegistry] = None,
                 service: Optional[GraphService] = None,
                 normalizer: Optional[Normalizer] = None,
                 model: Optional[str] = None,
                 timeout: float = MENTION_TIMEOUT) -> None:
        self.registry = registry or CanonicalEntityRegistry(service=service,
                                                            normalizer=normalizer)
        self._model = model
        self._timeout = timeout

    # ── mention detection ────────────────────────────────────────────────────

    def extract_mentions(self, question: str) -> tuple[list[str], float]:
        """Return (mentions, elapsed_ms). Failure yields an empty list, not an error."""
        text = (question or "").strip()[:MAX_QUESTION_CHARS]
        if not text:
            return [], 0.0

        started = time.perf_counter()
        raw = _llm.complete(
            [{"role": "user", "content": mention_prompt(text)}],
            model=self._model,
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
            timeout=self._timeout,
        )
        took = (time.perf_counter() - started) * 1000

        payload = _parse_json_object(raw)
        if payload is None:
            log.warning("kg resolver: mention extraction returned unparseable JSON")
            return [], took

        seen, mentions = set(), []
        for item in payload.get("mentions") or []:
            mention = str(item).strip()
            if mention and mention.lower() not in seen:
                seen.add(mention.lower())
                mentions.append(mention)
        return mentions, took

    # ── resolution ───────────────────────────────────────────────────────────

    def resolve_mentions(self, mentions: list[str], *,
                         allow_fuzzy: bool = True) -> list[ResolvedEntity]:
        """Bind each mention through the registry. Order is preserved."""
        self.registry.warm()
        out, seen_ids = [], set()
        for mention in mentions:
            resolved = self.registry.resolve(mention, allow_fuzzy=allow_fuzzy)
            # Two mentions can legitimately land on one entity ("MS Graph" and
            # "Microsoft Graph" in the same question); keep the first, drop the
            # duplicate rather than seeding the same node twice.
            if resolved.resolved and resolved.entity_id in seen_ids:
                continue
            if resolved.resolved:
                seen_ids.add(resolved.entity_id)
            out.append(resolved)
        return out

    def resolve(self, question: str, *, allow_fuzzy: bool = True
                ) -> tuple[list[ResolvedEntity], dict]:
        """Full path: question → mentions → canonical entities.

        Returns (entities, timings). Unresolved mentions come back too, with
        `resolved == False` and any near-miss candidates attached, so a caller
        can tell "nothing in the graph" from "extraction found nothing"."""
        mentions, extract_ms = self.extract_mentions(question)
        started = time.perf_counter()
        entities = self.resolve_mentions(mentions, allow_fuzzy=allow_fuzzy)
        resolve_ms = (time.perf_counter() - started) * 1000

        hits = sum(1 for e in entities if e.resolved)
        log.info("kg resolver: %d mention(s) → %d resolved (%.0fms extract, %.0fms resolve)",
                 len(mentions), hits, extract_ms, resolve_ms)
        return entities, {"extract_ms": extract_ms, "resolve_ms": resolve_ms,
                          "mentions_found": len(mentions), "mentions_resolved": hits}
