"""
GraphRetriever — canonical entities in, ranked evidence-carrying subgraph out.

Expansion is breadth-first, one hop per query, with the visited set carried into
each hop so a node is never re-expanded. That is why depth is configurable
without the query cost exploding: `depth=2` costs two round trips, not a
variable-length path match over the whole graph.

The hard rule of this module: **an edge with no evidence is not returned.** A
relationship whose provenance is empty cannot be attributed to any source, so
handing it back would let an unsupported claim look identical to a corroborated
one. Those edges are counted (`edges_dropped_unsupported`) rather than silently
discarded, so the count is visible if extraction ever stops writing provenance.

Nothing here generates text.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from ..service import GraphService, get_graph_service
from . import ranking
from .ranking import RankingWeights
from .types import Evidence, ScoredEdge, ScoredNode, Subgraph

log = logging.getLogger("aganeti.kg.retriever")

DEFAULT_MAX_NODES = 100
DEFAULT_FANOUT = 500            # per-hop edge ceiling, enforced in Cypher


class GraphRetriever:
    """Neighbourhood expansion with ranking and evidence."""

    def __init__(self, service: Optional[GraphService] = None,
                 weights: Optional[RankingWeights] = None,
                 max_nodes: int = DEFAULT_MAX_NODES,
                 fanout: int = DEFAULT_FANOUT,
                 require_evidence: bool = True) -> None:
        self._svc = service or get_graph_service()
        self.weights = weights or ranking.DEFAULT_WEIGHTS
        self.max_nodes = max_nodes
        self.fanout = fanout
        # Off only for inspecting a graph built before provenance existed.
        self.require_evidence = require_evidence
        self.queries = 0

    # ── expansion ────────────────────────────────────────────────────────────

    def expand(self, seed_ids: Iterable[str], depth: int = 1,
               rel_types: Optional[set[str]] = None) -> Subgraph:
        """Breadth-first expansion to `depth` hops from `seed_ids`.

        `depth=0` returns just the seeds (useful for "what do we know about X?"
        without pulling in the neighbourhood)."""
        seeds = [s for s in dict.fromkeys(seed_ids) if s]
        subgraph = Subgraph(seed_ids=list(seeds))
        if not seeds:
            return subgraph

        depth = max(0, int(depth))
        self.queries = 0
        nodes: dict[str, ScoredNode] = {}
        hop_of: dict[str, int] = {}

        # ── hop 0: the seeds themselves ──
        for row in self._fetch(seeds):
            node = self._to_scored(row["node"], row["labels"], row["degree"], hop=0)
            nodes[node.entity_id] = node
            hop_of[node.entity_id] = 0

        frontier = list(nodes.keys())
        edges: dict[tuple[str, str, str], ScoredEdge] = {}
        dropped = 0

        for hop in range(1, depth + 1):
            if not frontier or len(nodes) >= self.max_nodes:
                break
            visited = list(nodes.keys())
            try:
                rows = self._svc.expand_one_hop(frontier, visited, limit=self.fanout)
                self.queries += 1
            except Exception as e:  # noqa: BLE001 — a failed hop truncates, never crashes
                log.warning("kg retriever: hop %d failed: %s", hop, e)
                subgraph.truncated = True
                break

            next_frontier: list[str] = []
            for row in rows:
                if rel_types and row["rel_type"] not in rel_types:
                    continue
                evidence = Evidence.from_properties(row["rel_props"])
                if self.require_evidence and not evidence.is_supported:
                    dropped += 1
                    continue

                node_id = row["node"].id
                if node_id not in nodes:
                    if len(nodes) >= self.max_nodes:
                        subgraph.truncated = True
                        continue
                    scored = self._to_scored(row["node"], row["labels"],
                                             row["degree"], hop=hop)
                    nodes[node_id] = scored
                    hop_of[node_id] = hop
                    next_frontier.append(node_id)

                key = (row["start_id"], row["rel_type"], row["end_id"])
                if key not in edges:
                    edges[key] = ScoredEdge(
                        rel_type=row["rel_type"], start_id=row["start_id"],
                        end_id=row["end_id"], evidence=evidence,
                        properties={k: v for k, v in row["rel_props"].items()
                                    if not k.startswith(("source_", "add_"))},
                        score=ranking.score_edge(evidence, hop=hop - 1, weights=self.weights),
                    )
            frontier = next_frontier

        # ── close the subgraph: edges among everything we collected ──
        node_ids = list(nodes.keys())
        if len(node_ids) > 1:
            try:
                for row in self._svc.edges_between(node_ids):
                    evidence = Evidence.from_properties(row["rel_props"])
                    if self.require_evidence and not evidence.is_supported:
                        dropped += 1
                        continue
                    if rel_types and row["rel_type"] not in rel_types:
                        continue
                    key = (row["start_id"], row["rel_type"], row["end_id"])
                    if key in edges:
                        continue
                    hop = max(hop_of.get(row["start_id"], 0), hop_of.get(row["end_id"], 0))
                    edges[key] = ScoredEdge(
                        rel_type=row["rel_type"], start_id=row["start_id"],
                        end_id=row["end_id"], evidence=evidence,
                        properties={k: v for k, v in row["rel_props"].items()
                                    if not k.startswith(("source_", "add_"))},
                        score=ranking.score_edge(evidence, hop=hop, weights=self.weights),
                    )
                self.queries += 1
            except Exception as e:  # noqa: BLE001
                log.warning("kg retriever: induced-edge query failed: %s", e)

        subgraph.nodes = sorted(nodes.values(), key=lambda n: (-n.score, n.entity_id))
        subgraph.edges = sorted(edges.values(), key=lambda e: -e.score)
        subgraph.dropped_unsupported = dropped
        log.info("kg retriever: %d seed(s) → %d node(s), %d edge(s) over %d hop(s), "
                 "%d unsupported edge(s) dropped, %d quer(y/ies)",
                 len(seeds), len(subgraph.nodes), len(subgraph.edges), depth,
                 dropped, self.queries)
        return subgraph

    # ── helpers ──────────────────────────────────────────────────────────────

    def _fetch(self, ids: list[str]) -> list[dict]:
        try:
            rows = self._svc.fetch_entities(ids)
            self.queries += 1
            return rows
        except Exception as e:  # noqa: BLE001
            log.warning("kg retriever: seed fetch failed: %s", e)
            return []

    def _to_scored(self, node, labels: list[str], degree: int, hop: int) -> ScoredNode:
        props = node.properties
        evidence = Evidence.from_properties(props)
        score, signals = ranking.score_node(evidence, degree=degree, hop=hop,
                                            weights=self.weights)
        return ScoredNode(
            entity_id=node.id,
            canonical_name=props.get("canonical_name") or props.get("name") or node.id,
            labels=[l for l in labels if l != "Entity"],
            hop=hop, score=score, signals=signals, evidence=evidence,
            properties={k: v for k, v in props.items()
                        if k not in ("source_ids", "source_types", "user_ids",
                                     "conversation_ids", "message_ids", "document_ids",
                                     "models")},
        )
