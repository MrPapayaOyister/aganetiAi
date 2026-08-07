"""
LLM-driven extraction — entities first, relationships second.

Both extractors go through the LiteLLM gateway (backend/services/llm.py) with
`response_format={"type": "json_object"}`. No regex, no heuristics: the only
post-processing is validating the model's output against the closed vocabulary
in models.py and discarding anything outside it.

The two passes are independent and neither writes anything — persistence is the
builder's job. That separation is what lets you run extraction against a corpus
to evaluate quality without touching Neo4j.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from backend.services import llm as _llm

from . import schemas
from .models import NodeLabel, RelType
from .types import ExtractedEntity, ExtractedRelationship, ExtractionResult

log = logging.getLogger("aganeti.kg.extractor")

# Extraction is a background/batch operation, so a generous ceiling is fine;
# what we do not want is an unbounded wait inside someone's ingest loop.
try:
    from config.settings import KG_EXTRACT_TIMEOUT as _CFG_TIMEOUT, KG_EXTRACT_MODEL as _CFG_MODEL
except Exception:  # noqa: BLE001 — settings is optional for unit tests
    _CFG_TIMEOUT, _CFG_MODEL = 300.0, None
# Deadline for one extraction call. Paired with KG_EXTRACT_MODEL, which routes to
# a LiteLLM model whose OWN timeout is long enough to honour it — a client
# deadline above the gateway's just turns into an HTTP 408.
EXTRACT_TIMEOUT = _CFG_TIMEOUT
EXTRACT_MODEL = _CFG_MODEL
MAX_TEXT_CHARS = 12_000

_LABELS = {label.value.lower(): label.value for label in NodeLabel}
_REL_TYPES = {rel.value.lower(): rel.value for rel in RelType}

# Phrases a refusal opens with. Deliberately anchored to the START of the reply:
# a legitimate JSON extraction can easily CONTAIN "cannot" inside an entity name
# or a summary, but it does not begin with one of these.
_REFUSAL_OPENERS = (
    "i cannot", "i can't", "i am unable", "i'm unable", "i won't", "i will not",
    "as an ai", "sorry", "i apologize", "i apologise", "unable to comply",
    "i do not have", "i don't have",
)


def _looks_like_refusal(raw: str) -> bool:
    """True when the model answered in prose declining the task.

    Checked only AFTER JSON parsing has already failed, so a well-formed
    extraction can never be misread as a refusal."""
    head = (raw or "").strip().lstrip("\"'`*# ").lower()[:80]
    return any(head.startswith(opener) for opener in _REFUSAL_OPENERS)



def _parse_json_object(raw: str) -> Optional[dict]:
    """Parse the model's reply into a dict.

    `response_format=json_object` makes bare JSON overwhelmingly likely, but a
    model that ignores it tends to wrap the object in prose or a code fence — so
    fall back to the outermost {...} span rather than losing the whole extraction.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _coerce_label(value: Any) -> Optional[str]:
    """Map a model-supplied type onto a real NodeLabel, or None to drop it."""
    if not isinstance(value, str):
        return None
    return _LABELS.get(value.strip().lower())


def _coerce_rel_type(value: Any) -> Optional[str]:
    """Map a model-supplied relationship type onto a real RelType, or None."""
    if not isinstance(value, str):
        return None
    return _REL_TYPES.get(value.strip().replace(" ", "_").replace("-", "_").lower())


class EntityExtractor:
    """STEP 1 — natural language in, typed entities out."""

    def __init__(self, model: Optional[str] = None, timeout: float = EXTRACT_TIMEOUT) -> None:
        self._model = model                     # None → the gateway default (qwen-fast)
        self._timeout = timeout

    def extract(self, text: str) -> ExtractionResult:
        text = (text or "").strip()
        if not text:
            return ExtractionResult()
        if len(text) > MAX_TEXT_CHARS:
            log.info("kg: truncating input from %d to %d chars", len(text), MAX_TEXT_CHARS)
            text = text[:MAX_TEXT_CHARS]

        started = time.perf_counter()
        raw = _llm.complete(
            [{"role": "user", "content": schemas.entity_prompt(text)}],
            model=self._model,
            temperature=0.0,                    # extraction is not a creative task
            max_tokens=1200,
            response_format={"type": "json_object"},
            timeout=self._timeout,
        )
        took = (time.perf_counter() - started) * 1000

        payload = _parse_json_object(raw)
        if payload is None:
            log.warning("kg: entity extraction returned unparseable JSON (%d chars)", len(raw))
            return ExtractionResult(took_ms=took, error="unparseable_json")

        entities, dropped = [], []
        for item in payload.get("entities") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            label = _coerce_label(item.get("type"))
            if not name:
                continue
            if label is None:
                # An unknown label is a model error, not a new label: recording it
                # as Entity keeps the fact and loses only the classification.
                dropped.append(f"{item.get('type')}:{name}")
                label = NodeLabel.ENTITY.value
            entities.append(ExtractedEntity(name=name, type=label, raw_name=name))

        if dropped:
            log.info("kg: %d entities had an unknown type, filed as Entity: %s",
                     len(dropped), ", ".join(dropped[:5]))
        log.debug("kg: extracted %d entities in %.0fms", len(entities), took)
        return ExtractionResult(entities=entities, took_ms=took)


class RelationshipExtractor:
    """STEP 2 — relationships between entities that were ALREADY extracted.

    Grounding the prompt in the entity list is what keeps source/target
    resolvable; anything naming an unknown entity is dropped by the builder.
    """

    def __init__(self, model: Optional[str] = None, timeout: float = EXTRACT_TIMEOUT) -> None:
        self._model = model
        self._timeout = timeout

    def extract(self, text: str, entities: list[ExtractedEntity]) -> ExtractionResult:
        text = (text or "").strip()
        if not text or not entities:
            return ExtractionResult()
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]

        names = [e.name for e in entities]
        known = {n.lower(): n for n in names}

        started = time.perf_counter()
        raw = _llm.complete(
            [{"role": "user", "content": schemas.relationship_prompt(text, names)}],
            model=self._model,
            temperature=0.0,
            max_tokens=1200,
            response_format={"type": "json_object"},
            timeout=self._timeout,
        )
        took = (time.perf_counter() - started) * 1000

        payload = _parse_json_object(raw)
        if payload is None:
            log.warning("kg: relationship extraction returned unparseable JSON")
            return ExtractionResult(took_ms=took, error="unparseable_json")

        rels, dropped = [], []
        for item in payload.get("relationships") or []:
            if not isinstance(item, dict):
                continue
            src_raw = str(item.get("source") or "").strip()
            tgt_raw = str(item.get("target") or "").strip()
            rel_type = _coerce_rel_type(item.get("type"))
            if not src_raw or not tgt_raw:
                continue
            if rel_type is None:
                # Unknown verb: keep the connection, lose the nuance. Silently
                # dropping the edge loses more information than generalising it.
                dropped.append(f"{item.get('type')} ({src_raw}->{tgt_raw})")
                rel_type = RelType.RELATED_TO.value
            if src_raw.lower() == tgt_raw.lower():
                continue                        # self-loops carry no information here
            rels.append(ExtractedRelationship(
                source=known.get(src_raw.lower(), src_raw),
                type=rel_type,
                target=known.get(tgt_raw.lower(), tgt_raw),
                raw_source=src_raw, raw_target=tgt_raw,
                raw_type=str(item.get("type") or ""),
            ))

        if dropped:
            log.info("kg: %d relationships had an unknown type, filed as RELATED_TO: %s",
                     len(dropped), "; ".join(dropped[:5]))
        log.debug("kg: extracted %d relationships in %.0fms", len(rels), took)
        return ExtractionResult(relationships=rels, took_ms=took)


# ── Unified extractor (phase 2.5) ────────────────────────────────────────────

def _coerce_confidence(value: Any, default: float = 1.0) -> float:
    """Clamp a model-supplied confidence into [0,1]. Junk falls back to `default`."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class KnowledgeExtractor:
    """ONE LLM call returning entities, relationships, summary, keywords, confidence.

    Replaces the EntityExtractor → RelationshipExtractor pair. Both of those
    remain for backwards compatibility, but this is the path the pipeline uses:
    it halves latency and, more importantly, the model writes relationships while
    looking at the entity list it just produced, so endpoints match by
    construction rather than by prompt discipline.
    """

    def __init__(self, model: Optional[str] = None, timeout: float = EXTRACT_TIMEOUT,
                 max_tokens: int = 2000) -> None:
        # Falls back to KG_EXTRACT_MODEL (the long-deadline route), not the
        # gateway default — extraction on the 30s chat route times out.
        self._model = model or EXTRACT_MODEL
        self._timeout = timeout
        self._max_tokens = max_tokens

    @property
    def model_name(self) -> str:
        """What to record as provenance. Resolves the gateway default lazily."""
        if self._model:
            return self._model
        from config.settings import LLM_MODEL
        return LLM_MODEL

    def extract(self, text: str, source_type: str = "conversation") -> ExtractionResult:
        text = (text or "").strip()
        if not text:
            return ExtractionResult(llm_calls=0)
        if len(text) > MAX_TEXT_CHARS:
            log.info("kg: truncating input from %d to %d chars", len(text), MAX_TEXT_CHARS)
            text = text[:MAX_TEXT_CHARS]

        started = time.perf_counter()
        # raise_on_error=True: an empty string here is NOT a parse problem, and
        # conflating the two sent a previous batch chasing a JSON bug that was
        # really LiteLLM's 30s route timeout. Each cause now gets its own label so
        # the caller can decide whether retrying could possibly help.
        try:
            raw = _llm.complete(
                [{"role": "user", "content": schemas.unified_prompt(text, source_type)}],
                model=self._model,
                temperature=0.0,
                max_tokens=self._max_tokens,
                response_format={"type": "json_object"},
                timeout=self._timeout,
                raise_on_error=True,
            )
        except _llm.LLMTimeoutError as e:
            took = (time.perf_counter() - started) * 1000
            log.warning("kg: extraction timed out after %.0fms: %s", took, e)
            # Retryable: the same request may well succeed on a quieter gateway.
            return ExtractionResult(took_ms=took, error="timeout")
        except _llm.LLMEmptyResponseError:
            took = (time.perf_counter() - started) * 1000
            log.warning("kg: extraction returned an empty completion after %.0fms", took)
            return ExtractionResult(took_ms=took, error="empty_response")
        except _llm.LLMGatewayError as e:
            took = (time.perf_counter() - started) * 1000
            log.warning("kg: extraction gateway error after %.0fms: %s", took, e)
            # Not retryable in general — an identical request gets an identical 4xx.
            return ExtractionResult(took_ms=took, error="gateway_error")
        took = (time.perf_counter() - started) * 1000

        payload = _parse_json_object(raw)
        if payload is None:
            # The model answered, but not with JSON. A refusal ("I cannot…", "As an
            # AI…") is a distinct condition from malformed JSON: no amount of
            # retrying fixes a refusal, whereas a truncated object sometimes
            # succeeds on a second pass with the same prompt.
            if _looks_like_refusal(raw):
                log.warning("kg: extraction refused by the model (%d chars): %r",
                            len(raw), raw[:120])
                return ExtractionResult(took_ms=took, error="model_refusal")
            log.warning("kg: unified extraction returned unparseable JSON (%d chars)", len(raw))
            return ExtractionResult(took_ms=took, error="unparseable_json")

        entities = self._parse_entities(payload.get("entities"))
        known = {e.name.lower(): e.name for e in entities}
        # Aliases resolve too: a relationship naming "MS Graph" should still bind
        # to the entity that declared it as an alias.
        for entity in entities:
            for alias in entity.aliases:
                known.setdefault(alias.lower(), entity.name)
        relationships = self._parse_relationships(payload.get("relationships"), known)

        keywords = [str(k).strip() for k in (payload.get("keywords") or [])
                    if str(k).strip()][:25]
        result = ExtractionResult(
            entities=entities,
            relationships=relationships,
            took_ms=took,
            summary=str(payload.get("summary") or "").strip()[:2000],
            keywords=keywords,
            confidence=_coerce_confidence(payload.get("confidence")),
            llm_calls=1,
        )
        log.debug("kg: unified extraction — %d entities, %d relationships, %.0fms",
                  len(entities), len(relationships), took)
        return result

    # ── parsing ──────────────────────────────────────────────────────────────

    def _parse_entities(self, items: Any) -> list[ExtractedEntity]:
        out, unknown = [], []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            primary = _coerce_label(item.get("type"))
            if primary is None:
                unknown.append(f"{item.get('type')}:{name}")
                primary = NodeLabel.ENTITY.value

            secondary = []
            for extra in (item.get("also") or []):
                label = _coerce_label(extra)
                # Entity is the catch-all; carrying it as a secondary label adds
                # nothing and every node already gets the :Entity base label.
                if label and label != primary and label != NodeLabel.ENTITY.value:
                    secondary.append(label)

            aliases = [str(a).strip() for a in (item.get("aliases") or [])
                       if str(a).strip() and str(a).strip().lower() != name.lower()]

            out.append(ExtractedEntity(
                name=name, type=primary, raw_name=name,
                secondary_labels=sorted(set(secondary)),
                aliases=sorted(set(aliases)),
                confidence=_coerce_confidence(item.get("confidence")),
            ))
        if unknown:
            log.info("kg: %d entities had an unknown type, filed as Entity: %s",
                     len(unknown), ", ".join(unknown[:5]))
        return out

    def _parse_relationships(self, items: Any,
                             known: dict[str, str]) -> list[ExtractedRelationship]:
        out, unknown = [], []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            src_raw = str(item.get("source") or "").strip()
            tgt_raw = str(item.get("target") or "").strip()
            if not src_raw or not tgt_raw or src_raw.lower() == tgt_raw.lower():
                continue
            rel_type = _coerce_rel_type(item.get("type"))
            if rel_type is None:
                unknown.append(f"{item.get('type')} ({src_raw}->{tgt_raw})")
                rel_type = RelType.RELATED_TO.value
            out.append(ExtractedRelationship(
                source=known.get(src_raw.lower(), src_raw),
                type=rel_type,
                target=known.get(tgt_raw.lower(), tgt_raw),
                raw_source=src_raw, raw_target=tgt_raw,
                raw_type=str(item.get("type") or ""),
                confidence=_coerce_confidence(item.get("confidence")),
            ))
        if unknown:
            log.info("kg: %d relationships had an unknown type, filed as RELATED_TO: %s",
                     len(unknown), "; ".join(unknown[:5]))
        return out
