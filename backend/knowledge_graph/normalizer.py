"""
Normalization — turn many spellings of one thing into one canonical entity.

Without this, "M365", "Microsoft 365" and "microsoft 365" become three nodes and
the graph quietly fragments. The rules, in order:

  1. whitespace collapse and trim
  2. strip decoration ("the ", trailing punctuation)
  3. alias lookup, case-insensitive        ← the extension point
  4. casing policy for anything unaliased
  5. deduplicate, preferring the more specific label

Designed to be extended without editing this file: `register_alias`,
`register_aliases` and `load_aliases_from_file` all mutate the running map, and
CANONICAL_ALIASES itself is just a dict you can add to.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

from .models import NodeLabel
from .types import ExtractedEntity, ExtractedRelationship

log = logging.getLogger("aganeti.kg.normalizer")

# ── Alias map: lowercased variant → canonical form ───────────────────────────
# Only put things here whose canonical spelling is genuinely fixed. Anything
# ambiguous belongs in a per-tenant map loaded at runtime, not in the source.
CANONICAL_ALIASES: dict[str, str] = {
    # Microsoft
    "m365": "Microsoft 365",
    "ms365": "Microsoft 365",
    "o365": "Microsoft 365",
    "office 365": "Microsoft 365",
    "microsoft365": "Microsoft 365",
    "ms graph": "Microsoft Graph",
    "msgraph": "Microsoft Graph",
    "graph api": "Microsoft Graph",
    "microsoft graph api": "Microsoft Graph",
    "azure ad": "Microsoft Entra ID",
    "aad": "Microsoft Entra ID",
    "entra": "Microsoft Entra ID",
    "outlook 365": "Outlook",
    "ms teams": "Microsoft Teams",
    "teams": "Microsoft Teams",
    "ms": "Microsoft",
    "msft": "Microsoft",

    # Datastores
    "postgres": "PostgreSQL",
    "postgresql db": "PostgreSQL",
    "psql": "PostgreSQL",
    "pg": "PostgreSQL",
    "neo4j db": "Neo4j",
    "neo 4j": "Neo4j",
    "qdrant db": "Qdrant",

    # Models / inference
    "qwen fast": "qwen-fast",
    "qwen-fast model": "qwen-fast",
    "qwenfast": "qwen-fast",
    "qwen3": "qwen3-30b-a3b",
    "litellm proxy": "LiteLLM",
    "lite llm": "LiteLLM",
    "vllm server": "vLLM",
    "v-llm": "vLLM",
    "gpt4.1": "gpt-4.1",
    "gpt 4.1": "gpt-4.1",

    # Google
    "gmail api": "Gmail",
    "google workspace": "Google Workspace",
    "gcal": "Google Calendar",

    # This platform
    "agentic ai platform": "Agentic AI",
    "agenticai": "Agentic AI",
    "aganeti": "Agentic AI",
    "aria": "Agentic AI",

    # Misc technical
    "fast api": "FastAPI",
    "fastapi framework": "FastAPI",
    "oauth2": "OAuth 2.0",
    "oauth 2": "OAuth 2.0",
    "rest": "REST API",
}

# Tokens whose canonical casing is not title case. Anything matching here
# (case-insensitively) keeps the spelling on the right.
CASING_OVERRIDES: dict[str, str] = {
    "api": "API",
    "ai": "AI",
    "ui": "UI",
    "id": "ID",
    "sql": "SQL",
    "url": "URL",
    "sdk": "SDK",
    "llm": "LLM",
    "jwt": "JWT",
    "pr": "PR",
    "ci": "CI",
    "cd": "CD",
    "os": "OS",
    "db": "DB",
}

# A name that already carries deliberate internal capitalisation or symbols
# (LiteLLM, vLLM, qwen-fast, gpt-4.1, Neo4j) must never be re-cased.
_HAS_INTERNAL_CAPS = re.compile(r"[a-z][A-Z]")
_HAS_SYMBOLS = re.compile(r"[-_./0-9]")

_WS = re.compile(r"\s+")
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)
_TRAILING_PUNCT = re.compile(r"[\s.,;:!?'\"]+$")

# Label preference when the same name arrives with two types — more specific wins,
# so "Agentic AI" typed once as Entity and once as Project resolves to Project.
_LABEL_SPECIFICITY: dict[str, int] = {NodeLabel.ENTITY.value: 0}


class Normalizer:
    """Canonicalises entity names and keeps relationships pointing at them."""

    def __init__(self, aliases: Optional[dict[str, str]] = None,
                 casing: Optional[dict[str, str]] = None) -> None:
        # Copy so one instance's registrations never leak into another's.
        self._aliases = dict(CANONICAL_ALIASES)
        if aliases:
            self._aliases.update({k.lower().strip(): v for k, v in aliases.items()})
        self._casing = dict(CASING_OVERRIDES)
        if casing:
            self._casing.update({k.lower(): v for k, v in casing.items()})

    # ── extension points ─────────────────────────────────────────────────────

    def register_alias(self, variant: str, canonical: str) -> None:
        """Teach the normalizer one new spelling. Safe to call at runtime."""
        self._aliases[variant.lower().strip()] = canonical

    def register_aliases(self, mapping: dict[str, str]) -> None:
        for variant, canonical in mapping.items():
            self.register_alias(variant, canonical)

    def load_aliases_from_file(self, path: "str | Path") -> int:
        """Load a {"variant": "canonical"} JSON map. Returns how many were added.

        Lets a deployment carry its own vocabulary (customer names, internal
        service names) without a code change."""
        p = Path(path)
        if not p.exists():
            log.info("kg: alias file %s not found — using built-in map only", p)
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("kg: could not read alias file %s: %s", p, e)
            return 0
        if not isinstance(data, dict):
            log.warning("kg: alias file %s is not a JSON object", p)
            return 0
        self.register_aliases({str(k): str(v) for k, v in data.items()})
        log.info("kg: loaded %d aliases from %s", len(data), p)
        return len(data)

    # ── the rules ────────────────────────────────────────────────────────────

    def clean(self, name: str) -> str:
        """Whitespace, unicode and decoration only — no aliasing, no casing."""
        if not name:
            return ""
        # NFKC folds look-alike unicode (full-width, ligatures) so "ＡＰＩ" == "API".
        text = unicodedata.normalize("NFKC", str(name))
        text = _WS.sub(" ", text).strip()
        text = _TRAILING_PUNCT.sub("", text)
        text = _LEADING_ARTICLE.sub("", text).strip()
        return text

    def apply_casing(self, name: str) -> str:
        """Casing policy for names with no alias entry.

        Deliberately conservative: anything with internal capitals or symbols is
        left exactly as the model wrote it, because re-casing "LiteLLM" to
        "Litellm" or "qwen-fast" to "Qwen-Fast" destroys the real name."""
        if not name:
            return name
        if _HAS_INTERNAL_CAPS.search(name) or _HAS_SYMBOLS.search(name):
            return name
        if name.isupper() and len(name) <= 6:      # acronym: CRM, HTTP
            return name
        words = []
        for word in name.split(" "):
            override = self._casing.get(word.lower())
            words.append(override if override else
                         (word if word[:1].isupper() else word.capitalize()))
        return " ".join(words)

    def canonical_name(self, name: str) -> str:
        """Full pipeline for a single name: clean → alias → casing."""
        cleaned = self.clean(name)
        if not cleaned:
            return ""
        aliased = self._aliases.get(cleaned.lower())
        if aliased:
            return aliased
        return self.apply_casing(cleaned)

    # ── batch operations ─────────────────────────────────────────────────────

    def normalize_entities(self, entities: Iterable[ExtractedEntity]
                           ) -> tuple[list[ExtractedEntity], int, dict[str, str]]:
        """Canonicalise and deduplicate.

        Returns (entities, duplicates_removed, rename_map). `rename_map` maps the
        original name to the canonical one so relationships can be repointed
        without re-running the LLM.
        """
        out: dict[tuple[str, str], ExtractedEntity] = {}
        by_name: dict[str, ExtractedEntity] = {}
        rename: dict[str, str] = {}
        duplicates = 0

        for entity in entities:
            canonical = self.canonical_name(entity.name)
            if not canonical:
                continue
            rename[entity.name] = canonical
            rename[entity.name.lower()] = canonical

            existing = by_name.get(canonical.lower())
            if existing is not None:
                duplicates += 1
                # Same name, two labels → keep the more specific one.
                if (_LABEL_SPECIFICITY.get(entity.type, 1)
                        > _LABEL_SPECIFICITY.get(existing.type, 1)):
                    existing.type = entity.type
                existing.properties.update(entity.properties)
                continue

            normalized = ExtractedEntity(
                name=canonical, type=entity.type,
                raw_name=entity.raw_name or entity.name,
                properties=dict(entity.properties),
            )
            key = (canonical.lower(), entity.type)
            out[key] = normalized
            by_name[canonical.lower()] = normalized

        return list(out.values()), duplicates, rename

    def normalize_relationships(self, relationships: Iterable[ExtractedRelationship],
                                rename: Optional[dict[str, str]] = None
                                ) -> list[ExtractedRelationship]:
        """Repoint endpoints at canonical names and drop duplicate edges."""
        rename = rename or {}
        seen: set[tuple[str, str, str]] = set()
        out: list[ExtractedRelationship] = []

        for rel in relationships:
            source = rename.get(rel.source) or rename.get(rel.source.lower()) \
                or self.canonical_name(rel.source)
            target = rename.get(rel.target) or rename.get(rel.target.lower()) \
                or self.canonical_name(rel.target)
            if not source or not target or source.lower() == target.lower():
                continue
            key = (source.lower(), rel.type, target.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(ExtractedRelationship(
                source=source, type=rel.type, target=target,
                raw_source=rel.raw_source or rel.source,
                raw_target=rel.raw_target or rel.target,
                raw_type=rel.raw_type or rel.type,
                properties=dict(rel.properties),
            ))
        return out

    # ── Phase 2.5: alias resolution + label merging ───────────────────────────

    def resolve(self, entities: "Iterable[ExtractedEntity]"
                ) -> "tuple[list[ExtractedEntity], int, dict[str, str]]":
        """Canonicalise, then MERGE BY IDENTITY rather than by (name, label).

        This is the fix for the phase-2 duplicate-node problem. Previously the
        dedupe key was (name, label), so "Microsoft Graph" as Technology and as
        API were two entities and became two nodes. Here the key is the canonical
        name alone: one entity survives, the losing entity's label is folded into
        `secondary_labels`, and its aliases and confidence are merged in.

        Returns (entities, merged_count, rename_map). `rename_map` lets
        relationships be repointed without another LLM call — and it also maps
        every declared ALIAS onto the canonical name, so an edge naming
        "MS Graph" still binds correctly.
        """
        by_key: dict[str, ExtractedEntity] = {}
        rename: dict[str, str] = {}
        merged = 0

        for entity in entities:
            canonical = self.canonical_name(entity.name)
            if not canonical:
                continue

            # Alias spellings the model reported, canonicalised the same way.
            alias_forms = {self.canonical_name(a) for a in entity.aliases}
            alias_forms = {a for a in alias_forms if a and a.lower() != canonical.lower()}

            for form in (entity.name, entity.raw_name, canonical, *entity.aliases):
                if form:
                    rename[form] = canonical
                    rename[form.lower()] = canonical

            key = canonical.lower()
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = ExtractedEntity(
                    name=canonical, type=entity.type,
                    raw_name=entity.raw_name or entity.name,
                    canonical_name=canonical,
                    aliases=sorted(alias_forms | self._recorded_variants(entity, canonical)),
                    secondary_labels=list(entity.secondary_labels),
                    confidence=entity.confidence,
                    properties=dict(entity.properties),
                )
                continue

            # ── same thing, seen again: fold rather than duplicate ──
            merged += 1
            labels = set(existing.all_labels) | set(entity.all_labels)
            primary = self._preferred_primary(existing.type, entity.type)
            existing.type = primary
            existing.secondary_labels = sorted(l for l in labels if l != primary)
            existing.aliases = sorted(set(existing.aliases) | alias_forms
                                      | self._recorded_variants(entity, canonical))
            # Max, not latest: a second low-confidence mention should not erase a
            # confident first one (same rule the graph applies on merge).
            stated = [c for c in (existing.confidence, entity.confidence) if c is not None]
            existing.confidence = max(stated) if stated else None
            existing.properties.update(entity.properties)

        return list(by_key.values()), merged, rename

    @staticmethod
    def _recorded_variants(entity: "ExtractedEntity", canonical: str) -> set:
        """Original spellings worth keeping as aliases once the name has changed.

        If the text said "MS Graph" and we canonicalised to "Microsoft Graph",
        the original is the single most useful alias to store — it is how this
        corpus actually refers to the thing."""
        out = set()
        for form in (entity.name, entity.raw_name):
            if form and form.lower() != canonical.lower():
                out.add(form)
        return out

    @staticmethod
    def _preferred_primary(current: str, candidate: str) -> str:
        """Which label leads when one entity arrives with two.

        `Entity` is the catch-all and always loses. Otherwise the incumbent wins:
        arrival order is arbitrary, so churn is worse than an arbitrary-but-stable
        choice, and the loser is not discarded — it becomes a secondary label."""
        entity_label = NodeLabel.ENTITY.value
        if current == entity_label and candidate != entity_label:
            return candidate
        return current
