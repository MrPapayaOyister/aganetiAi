"""
ContextCompressor — algorithmic shrinking. No LLM, ever.

Graph retrieval can return a hundred nodes and edges for one question. Feeding
that to a model wastes budget on repetition and buries the few items that
matter. Compression removes redundancy WITHOUT an LLM call — a summarisation
call would add latency, cost and a hallucination surface to a context pipeline
whose entire job is to be trustworthy.

Four passes, cheapest first:

  1. exact-duplicate removal        identical normalised text
  2. entity collapsing              many edges on one entity → one line
  3. chain collapsing               A→B, B→C, C→D → A→B→C→D
  4. subsumption                    drop items wholly contained in a kept one

Every pass preserves evidence: a collapsed item's attestations move onto the
survivor, so corroboration counts never shrink just because the text got shorter.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from .bundle import ContextItem
from .evidence import corroboration_of, merge_evidence, providers_of
from .fusion import containment

log = logging.getLogger("aganeti.context.compression")

_WS = re.compile(r"\s+")


def _norm_text(text: str) -> str:
    return _WS.sub(" ", (text or "").strip().lower())


@dataclass(slots=True)
class CompressionResult:
    items: "list[ContextItem]"
    removed_exact: int = 0
    collapsed_entities: int = 0
    collapsed_chains: int = 0
    removed_subsumed: int = 0
    took_ms: float = 0.0

    @property
    def total_removed(self) -> int:
        return (self.removed_exact + self.collapsed_entities
                + self.collapsed_chains + self.removed_subsumed)


class ContextCompressor:
    """Pure-algorithmic redundancy removal."""

    def __init__(self, *, collapse_entities: bool = True,
                 collapse_chains: bool = True,
                 drop_subsumed: bool = True,
                 min_group: int = 3,
                 subsumption_threshold: float = 0.95) -> None:
        self.collapse_entities = collapse_entities
        self.collapse_chains = collapse_chains
        self.drop_subsumed = drop_subsumed
        # Below this, collapsing hurts: two edges read better as two lines than
        # as a synthesised summary line.
        self.min_group = min_group
        self.subsumption_threshold = subsumption_threshold

    # ── 1. exact duplicates ──────────────────────────────────────────────────

    @staticmethod
    def _dedupe_exact(items: "list[ContextItem]") -> "tuple[list[ContextItem], int]":
        seen: dict[str, ContextItem] = {}
        out, removed = [], 0
        for item in items:
            key = _norm_text(item.text)
            if key in seen:
                keeper = seen[key]
                keeper.evidence = merge_evidence([keeper.evidence, item.evidence])
                keeper.providers = providers_of(keeper.evidence)
                keeper.corroboration = corroboration_of(keeper.evidence)
                removed += 1
                continue
            seen[key] = item
            out.append(item)
        return out, removed

    # ── 2. entity collapsing ─────────────────────────────────────────────────

    def _collapse_entities(self, items: "list[ContextItem]"
                           ) -> "tuple[list[ContextItem], int]":
        """Many relationships sharing a subject become one line.

            agentic-ai —USES→ litellm
            agentic-ai —USES→ microsoft-graph      →  agentic-ai —USES→ litellm,
            agentic-ai —STORES→ postgresql            microsoft-graph; —STORES→ postgresql

        Only graph relationship items participate; prose is never rewritten.
        """
        groups: dict[str, list[ContextItem]] = defaultdict(list)
        passthrough: list[ContextItem] = []
        for item in items:
            meta = item.metadata or {}
            if item.provider == "graph" and meta.get("kind") == "relationship" \
                    and meta.get("start_id"):
                groups[str(meta["start_id"])].append(item)
            else:
                passthrough.append(item)

        out, collapsed = list(passthrough), 0
        for start_id, group in groups.items():
            if len(group) < self.min_group:
                out.extend(group)
                continue
            by_rel: dict[str, list[str]] = defaultdict(list)
            for g in group:
                by_rel[str(g.metadata.get("rel_type", "RELATED_TO"))].append(
                    str(g.metadata.get("end_id", "")))
            parts = [f"—{rel}→ " + ", ".join(dict.fromkeys(t for t in targets if t))
                     for rel, targets in by_rel.items()]
            keeper = max(group, key=lambda g: g.normalized_score)
            keeper.text = f"{start_id} " + "; ".join(parts)
            keeper.evidence = merge_evidence([g.evidence for g in group])
            keeper.providers = providers_of(keeper.evidence)
            keeper.corroboration = corroboration_of(keeper.evidence)
            keeper.normalized_score = max(g.normalized_score for g in group)
            keeper.metadata = {**keeper.metadata, "kind": "entity_summary",
                               "collapsed": len(group), "start_id": start_id}
            out.append(keeper)
            collapsed += len(group) - 1
        return out, collapsed

    # ── 3. chain collapsing ──────────────────────────────────────────────────

    def _collapse_chains(self, items: "list[ContextItem]"
                         ) -> "tuple[list[ContextItem], int]":
        """A→B, B→C, C→D become one path line.

        Only unambiguous links are joined: a node that appears as the start of
        two different edges forks the path, and a fork rendered as one chain
        would assert a route that does not exist."""
        rels = [i for i in items
                if i.provider == "graph"
                and (i.metadata or {}).get("kind") == "relationship"
                and i.metadata.get("start_id") and i.metadata.get("end_id")]
        if len(rels) < 2:
            return items, 0

        by_start: dict[str, list[ContextItem]] = defaultdict(list)
        in_degree: dict[str, int] = defaultdict(int)
        for r in rels:
            by_start[str(r.metadata["start_id"])].append(r)
            in_degree[str(r.metadata["end_id"])] += 1

        used: set[int] = set()
        chains: list[list[ContextItem]] = []
        for r in rels:
            start = str(r.metadata["start_id"])
            if id(r) in used or in_degree[start] > 0:
                continue                       # not a chain head
            chain, node = [], start
            while True:
                nxt = by_start.get(node, [])
                if len(nxt) != 1 or id(nxt[0]) in used:
                    break
                edge = nxt[0]
                used.add(id(edge))
                chain.append(edge)
                node = str(edge.metadata["end_id"])
            if len(chain) >= 2:
                chains.append(chain)
            else:
                for e in chain:
                    used.discard(id(e))

        if not chains:
            return items, 0

        collapsed_ids = {id(e) for c in chains for e in c}
        out = [i for i in items if id(i) not in collapsed_ids]
        collapsed = 0
        for chain in chains:
            path = str(chain[0].metadata["start_id"])
            for edge in chain:
                path += f" —{edge.metadata.get('rel_type','RELATED_TO')}→ {edge.metadata['end_id']}"
            keeper = max(chain, key=lambda e: e.normalized_score)
            keeper.text = path
            keeper.evidence = merge_evidence([e.evidence for e in chain])
            keeper.providers = providers_of(keeper.evidence)
            keeper.corroboration = corroboration_of(keeper.evidence)
            keeper.normalized_score = max(e.normalized_score for e in chain)
            keeper.metadata = {**keeper.metadata, "kind": "chain",
                               "chain_length": len(chain)}
            out.append(keeper)
            collapsed += len(chain) - 1
        return out, collapsed

    # ── 4. subsumption ───────────────────────────────────────────────────────

    def _drop_subsumed(self, items: "list[ContextItem]"
                       ) -> "tuple[list[ContextItem], int]":
        """Drop an item whose content is already inside a higher-ranked one."""
        ordered = sorted(items, key=lambda i: -len(i.text))
        kept: list[ContextItem] = []
        removed = 0
        for item in ordered:
            host = next((k for k in kept
                         if len(k.text) > len(item.text)
                         and containment(k.text, item.text) >= self.subsumption_threshold),
                        None)
            if host is None:
                kept.append(item)
                continue
            host.evidence = merge_evidence([host.evidence, item.evidence])
            host.providers = providers_of(host.evidence)
            host.corroboration = corroboration_of(host.evidence)
            removed += 1
        return kept, removed

    # ── entry point ──────────────────────────────────────────────────────────

    def compress(self, items: "list[ContextItem]") -> CompressionResult:
        started = time.perf_counter()
        result = CompressionResult(items=list(items))

        result.items, result.removed_exact = self._dedupe_exact(result.items)
        if self.collapse_chains:
            result.items, result.collapsed_chains = self._collapse_chains(result.items)
        if self.collapse_entities:
            result.items, result.collapsed_entities = self._collapse_entities(result.items)
        if self.drop_subsumed:
            result.items, result.removed_subsumed = self._drop_subsumed(result.items)

        result.took_ms = (time.perf_counter() - started) * 1000
        if result.total_removed:
            log.debug("compression: %d → %d items (exact=%d chains=%d entities=%d "
                      "subsumed=%d) in %.1fms", len(items), len(result.items),
                      result.removed_exact, result.collapsed_chains,
                      result.collapsed_entities, result.removed_subsumed, result.took_ms)
        return result
