"""
CanonicalEntityRegistry — the thing that decides "is this the same entity?".

Phase 2.5 could only merge aliases it saw inside a SINGLE extraction. Anything
learned yesterday was invisible today, so the graph fragmented as it grew. The
registry closes that: it resolves a name against the whole database, remembers
what it learned, and persists the mapping so the next process does not relearn it.

Resolution ladder, cheapest and most certain first:

    1. cache            in-memory alias → id (warmed from the graph)
    2. normalizer       the static alias map (M365 → Microsoft 365)
    3. exact id         the name slugifies to a known node id
    4. exact name       canonical_name or a stored alias matches, case-insensitively
    5. fuzzy            full-text index, then a similarity floor

Each rung is recorded as `match_type` so a caller can tell a certain hit from a
guess. Fuzzy matches below `fuzzy_threshold` are returned as *candidates*, never
as a resolution — silently binding a question to the wrong entity is far worse
than admitting it is unresolved.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

from ..builder import slugify
from ..normalizer import Normalizer
from ..service import GraphService, get_graph_service
from .types import Evidence, ResolvedEntity

log = logging.getLogger("aganeti.kg.registry")

# Lucene reserves these; an unescaped one either errors or quietly changes the
# query's meaning, so user text is never passed through raw.
_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')

DEFAULT_ALIAS_FILE = Path(__file__).resolve().parents[3] / "data_vault" / "kg_aliases.json"


def escape_lucene(text: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", text)


def similarity(a: str, b: str) -> float:
    """Cheap ratio in [0,1]. Deliberately stdlib — a fuzzy *ranking* signal here
    only re-orders candidates the database already returned."""
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


class CanonicalEntityRegistry:
    """Alias → canonical id, backed by Neo4j and a persisted alias table."""

    def __init__(self, service: Optional[GraphService] = None,
                 normalizer: Optional[Normalizer] = None,
                 alias_file: "str | Path | None" = DEFAULT_ALIAS_FILE,
                 fuzzy_threshold: float = 0.82) -> None:
        self._svc = service or get_graph_service()
        self._normalizer = normalizer or Normalizer()
        self._alias_file = Path(alias_file) if alias_file else None
        self.fuzzy_threshold = fuzzy_threshold

        # alias (lowercased) → canonical entity id
        self._cache: dict[str, str] = {}
        self._names: dict[str, str] = {}        # id → canonical_name
        self._lock = threading.Lock()
        self._warmed = False

        if self._alias_file:
            self.load_alias_table()

    # ── persistence ──────────────────────────────────────────────────────────

    def load_alias_table(self) -> int:
        """Load the persisted alias table. Missing file is normal, not an error."""
        if not self._alias_file or not self._alias_file.exists():
            return 0
        try:
            data = json.loads(self._alias_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("kg registry: cannot read alias table %s: %s", self._alias_file, e)
            return 0
        pairs = data.get("aliases", data) if isinstance(data, dict) else {}
        with self._lock:
            for alias, entity_id in pairs.items():
                self._cache[str(alias).lower()] = str(entity_id)
        log.info("kg registry: loaded %d alias(es) from %s", len(pairs), self._alias_file)
        return len(pairs)

    def save_alias_table(self) -> int:
        """Persist the alias table so the mapping survives a restart."""
        if not self._alias_file:
            return 0
        try:
            self._alias_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._alias_file.with_suffix(".tmp")
            with self._lock:
                payload = {"aliases": dict(self._cache), "names": dict(self._names)}
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._alias_file)
        except OSError as e:
            log.warning("kg registry: cannot write alias table: %s", e)
            return 0
        return len(payload["aliases"])

    # ── cache ────────────────────────────────────────────────────────────────

    def warm(self, force: bool = False) -> int:
        """Pull every known alias out of the graph into the cache.

        One query instead of a database round trip per lookup. Call it once per
        process; `force=True` after a large ingest."""
        if self._warmed and not force:
            return len(self._cache)
        try:
            rows = self._svc.all_alias_pairs()
        except Exception as e:  # noqa: BLE001 — a cold cache must not break resolution
            log.warning("kg registry: warm failed, falling back to per-lookup queries: %s", e)
            return len(self._cache)
        with self._lock:
            for row in rows:
                entity_id, name = row["id"], row.get("canonical_name") or ""
                if name:
                    self._cache[name.lower()] = entity_id
                    self._names[entity_id] = name
                for alias in row.get("aliases") or []:
                    self._cache.setdefault(str(alias).lower(), entity_id)
            self._warmed = True
        log.info("kg registry: warmed %d alias(es) for %d entities", len(self._cache), len(rows))
        return len(self._cache)

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
            self._names.clear()
            self._warmed = False

    # ── registration ─────────────────────────────────────────────────────────

    def register_alias(self, alias: str, entity_id: str, *, persist: bool = True) -> bool:
        """Teach the registry that `alias` means `entity_id`.

        Writes to the node too, so the knowledge survives a cache flush and shows
        up in the full-text index."""
        alias = (alias or "").strip()
        if not alias or not entity_id:
            return False
        with self._lock:
            self._cache[alias.lower()] = entity_id
        if persist:
            try:
                self._svc.add_aliases(entity_id, [alias])
            except Exception as e:  # noqa: BLE001
                log.warning("kg registry: could not persist alias %r → %s: %s",
                            alias, entity_id, e)
                return False
            self.save_alias_table()
        return True

    def register_aliases(self, mapping: dict[str, str]) -> int:
        count = sum(1 for a, e in mapping.items() if self.register_alias(a, e, persist=False))
        for entity_id in set(mapping.values()):
            aliases = [a for a, e in mapping.items() if e == entity_id]
            try:
                self._svc.add_aliases(entity_id, aliases)
            except Exception as e:  # noqa: BLE001
                log.warning("kg registry: bulk alias persist failed for %s: %s", entity_id, e)
        self.save_alias_table()
        return count

    # ── resolution ───────────────────────────────────────────────────────────

    def resolve(self, mention: str, *, allow_fuzzy: bool = True) -> ResolvedEntity:
        """Bind one mention to a canonical entity. Never raises."""
        raw = (mention or "").strip()
        if not raw:
            return ResolvedEntity(mention=mention or "")

        canonical = self._normalizer.canonical_name(raw)
        out = ResolvedEntity(mention=raw)

        # 1 + 2. cache (which already includes the normalizer's static aliases)
        for probe, match_type in ((canonical, "registry"), (raw, "registry")):
            hit = self._cache.get(probe.lower()) if probe else None
            if hit:
                return self._hydrate(out, hit, match_type,
                                     1.0 if probe.lower() == canonical.lower() else 0.95)

        # 3. the name slugifies straight onto a node id
        slug = slugify(canonical or raw)
        if slug:
            try:
                found = self._svc.find_entity(slug)
            except Exception as e:  # noqa: BLE001
                log.warning("kg registry: id lookup failed for %r: %s", slug, e)
                found = None
            if found:
                node, labels = found
                return self._fill(out, node, labels, "exact", 1.0)

        # 4. exact canonical_name / alias match in the database
        try:
            matches = self._svc.find_entities_by_name(canonical or raw, limit=5)
        except Exception as e:  # noqa: BLE001
            log.warning("kg registry: name lookup failed for %r: %s", raw, e)
            matches = []
        if matches:
            node, labels = matches[0]
            return self._fill(out, node, labels, "alias", 0.98)

        # 5. fuzzy, via the full-text index
        if allow_fuzzy:
            candidates = self.fuzzy_candidates(canonical or raw)
            out.candidates = candidates
            if candidates and candidates[0]["similarity"] >= self.fuzzy_threshold:
                best = candidates[0]
                out.entity_id = best["id"]
                out.canonical_name = best["canonical_name"]
                out.labels = best["labels"]
                out.aliases = best["aliases"]
                out.match_type = "fuzzy"
                out.score = best["similarity"]
                out.evidence = best.get("evidence")
                return out

        return out                              # unresolved; candidates may be set

    def fuzzy_candidates(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        """Full-text hits re-scored by string similarity.

        Lucene ranks by term statistics, which is the wrong objective here — it
        happily puts a long document-ish name above a near-exact short one. The
        re-score fixes the ordering; Lucene's job is just to narrow the field."""
        term = escape_lucene((name or "").strip())
        if not term:
            return []
        # `~` is Lucene's fuzzy operator: tolerates a typo or two per token.
        query = " OR ".join(f"{t}~" for t in term.split() if t) or term
        try:
            hits = self._svc.fulltext_search(query, limit=limit)
        except Exception as e:  # noqa: BLE001 — a missing index must degrade, not crash
            log.info("kg registry: fuzzy search unavailable (%s) — exact matching only", e)
            return []

        out = []
        for hit in hits:
            node, labels = hit["node"], hit["labels"]
            props = node.properties
            canonical_name = props.get("canonical_name") or props.get("name") or ""
            best = max([similarity(name, canonical_name)]
                       + [similarity(name, a) for a in (props.get("aliases") or [])],
                       default=0.0)
            out.append({
                "id": node.id,
                "canonical_name": canonical_name,
                "labels": [l for l in labels if l != "Entity"],
                "aliases": list(props.get("aliases") or []),
                "lucene_score": hit["score"],
                "similarity": round(best, 4),
                "evidence": Evidence.from_properties(props),
            })
        out.sort(key=lambda c: (-c["similarity"], -c["lucene_score"]))
        return out

    # ── historical alias merging ─────────────────────────────────────────────

    def find_legacy_entities(self) -> list[dict[str, Any]]:
        """Phase-2 nodes still on the `label:slug` id scheme."""
        try:
            return self._svc.find_legacy_scheme_nodes()
        except Exception as e:  # noqa: BLE001
            log.warning("kg registry: legacy scan failed: %s", e)
            return []

    def merge_historical_aliases(self, *, dry_run: bool = True) -> dict[str, Any]:
        """Fold phase-2 `label:slug` nodes into their phase-2.5 counterparts.

        Defaults to `dry_run=True`: this rewrites real data, so the caller has to
        ask for it explicitly. Only the ALIAS mapping is registered — no node is
        deleted and no edge is moved, because relinking edges is a migration
        decision rather than something a lookup helper should do silently.
        """
        legacy = self.find_legacy_entities()
        planned, skipped = [], []
        for row in legacy:
            old_id = row["id"]
            name = row.get("canonical_name") or row.get("name") or ""
            if not name:
                skipped.append(f"{old_id} (no name to re-key from)")
                continue
            new_id = slugify(self._normalizer.canonical_name(name))
            if not new_id or new_id == old_id:
                skipped.append(f"{old_id} (already canonical)")
                continue
            planned.append({"legacy_id": old_id, "canonical_id": new_id, "name": name})

        if not dry_run:
            for item in planned:
                # The legacy id becomes an alias of the canonical entity, so a
                # future mention of either resolves to one place.
                self.register_alias(item["legacy_id"], item["canonical_id"], persist=False)
                self.register_alias(item["name"], item["canonical_id"], persist=False)
            self.save_alias_table()

        return {"legacy_found": len(legacy), "planned": planned,
                "skipped": skipped, "applied": not dry_run}

    # ── helpers ──────────────────────────────────────────────────────────────

    def _hydrate(self, out: ResolvedEntity, entity_id: str,
                 match_type: str, score: float) -> ResolvedEntity:
        """Cache hit → full entity. Falls back to the cached name if the node is
        gone (a stale cache entry must not fabricate a resolution)."""
        try:
            found = self._svc.find_entity(entity_id)
        except Exception:  # noqa: BLE001
            found = None
        if not found:
            with self._lock:
                self._cache.pop(out.mention.lower(), None)
            return out
        node, labels = found
        return self._fill(out, node, labels, match_type, score)

    @staticmethod
    def _fill(out: ResolvedEntity, node: Any, labels: list[str],
              match_type: str, score: float) -> ResolvedEntity:
        props = node.properties
        out.entity_id = node.id
        out.canonical_name = props.get("canonical_name") or props.get("name") or node.id
        out.labels = [l for l in labels if l != "Entity"]
        out.aliases = list(props.get("aliases") or [])
        out.match_type = match_type
        out.score = score
        out.evidence = Evidence.from_properties(props)
        return out
