"""
GraphService — the ONLY place Cypher is executed.

Nothing outside this class runs a statement against Neo4j. That boundary is what
makes the graph auditable: every query is timed, every failure is logged with the
same shape, and the parameter/label split from queries.py is enforced in one
place instead of at N call sites.

Usage:

    from backend.graph import GraphService
    svc = GraphService()                       # shared process-wide driver
    svc.merge_node(NodeLabel.USER, "user_1", {"email": "a@b.com"})

Every method is synchronous. The Neo4j driver's sync API is thread-safe and this
is a side-car store — call it from a worker thread (`asyncio.to_thread`) if you
ever need it inside a request handler, exactly as the mail poll does.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from neo4j import Driver
from neo4j.exceptions import ConstraintError, Neo4jError, ServiceUnavailable

from . import queries as q
from .client import GraphUnavailable, database, get_driver
from .models import (
    GraphNode,
    GraphRelationship,
    Neighbor,
    NodeLabel,
    QueryStats,
    RelType,
    validate_label,
    validate_rel_type,
)

log = logging.getLogger("aganeti.graph.service")

# Queries slower than this are logged at WARNING rather than DEBUG, so a missing
# index shows up in the log without having to turn on debug logging.
SLOW_QUERY_MS = 250.0


class GraphService:
    """Typed, parameterized access to the graph."""

    def __init__(self, driver: Optional[Driver] = None, db: Optional[str] = None) -> None:
        """`driver=None` uses the shared process-wide pool (the normal case).
        Passing one explicitly is for tests that need an isolated database."""
        self._driver = driver
        self._db = db or database()

    # ── plumbing ─────────────────────────────────────────────────────────────

    @property
    def driver(self) -> Driver:
        """Resolved lazily so constructing a GraphService never opens a socket."""
        return self._driver if self._driver is not None else get_driver()

    def _execute(self, cypher: str, params: dict[str, Any] | None = None,
                 *, op: str = "query") -> Any:
        """Run a statement, timing it and normalising failures.

        Returns the driver's EagerResult (`.records`, `.summary`, `.keys`)."""
        params = params or {}
        started = time.perf_counter()
        try:
            result = self.driver.execute_query(cypher, parameters_=params, database_=self._db)
        except ServiceUnavailable as e:
            took = (time.perf_counter() - started) * 1000
            log.error("graph %s FAILED after %.1fms — Neo4j unavailable: %s", op, took, e)
            raise GraphUnavailable(f"Neo4j unavailable during {op}") from e
        except ConstraintError as e:
            took = (time.perf_counter() - started) * 1000
            # Distinct from a generic error: this means the caller violated a
            # uniqueness constraint, i.e. used create_* where merge_* was meant.
            log.warning("graph %s rejected after %.1fms — constraint violation: %s", op, took, e)
            raise
        except Neo4jError as e:
            took = (time.perf_counter() - started) * 1000
            log.error("graph %s FAILED after %.1fms — %s: %s", op, took, e.code, e.message)
            raise

        took = (time.perf_counter() - started) * 1000
        counters = result.summary.counters
        changed = (counters.nodes_created or counters.nodes_deleted
                   or counters.relationships_created or counters.relationships_deleted
                   or counters.properties_set)
        level = logging.WARNING if took >= SLOW_QUERY_MS else logging.DEBUG
        log.log(level, "graph %s took %.1fms (records=%d%s)", op, took, len(result.records),
                f", changes={changed}" if changed else "")
        return result

    @staticmethod
    def _stats(result: Any, took_ms: Optional[float] = None) -> QueryStats:
        c = result.summary.counters
        return QueryStats(
            nodes_created=c.nodes_created,
            nodes_deleted=c.nodes_deleted,
            relationships_created=c.relationships_created,
            relationships_deleted=c.relationships_deleted,
            properties_set=c.properties_set,
            took_ms=took_ms if took_ms is not None else result.summary.result_available_after,
        )

    # ── node writes ──────────────────────────────────────────────────────────

    def create_node(self, label: "str | NodeLabel", node_id: str,
                    properties: dict[str, Any] | None = None) -> GraphNode:
        """Create a node. Raises ConstraintError if `node_id` already exists."""
        label = validate_label(label)
        res = self._execute(q.create_node(label),
                            {"id": node_id, "props": properties or {}},
                            op=f"create_node:{label}")
        return GraphNode.from_record(res.records[0]["n"])

    def merge_node(self, label: "str | NodeLabel", node_id: str,
                   properties: dict[str, Any] | None = None) -> GraphNode:
        """Upsert a node by business key. Idempotent — the normal write path."""
        return self.merge_node_with_stats(label, node_id, properties)[0]

    def merge_node_with_stats(self, label: "str | NodeLabel", node_id: str,
                              properties: dict[str, Any] | None = None
                              ) -> "tuple[GraphNode, QueryStats]":
        """`merge_node` plus what the write actually did.

        The counters are the only reliable way to tell a create from a match:
        MERGE returns the node either way, so a caller that wants to report
        "3 new, 5 existing" would otherwise need a second round-trip per node."""
        label = validate_label(label)
        res = self._execute(q.merge_node(label),
                            {"id": node_id, "props": properties or {}},
                            op=f"merge_node:{label}")
        return GraphNode.from_record(res.records[0]["n"]), self._stats(res)

    def delete_node(self, label: "str | NodeLabel", node_id: str) -> QueryStats:
        """Delete a node AND its relationships (DETACH DELETE). No-op if absent."""
        label = validate_label(label)
        res = self._execute(q.delete_node(label), {"id": node_id},
                            op=f"delete_node:{label}")
        return self._stats(res)

    # ── node reads ───────────────────────────────────────────────────────────

    def find_node(self, label: "str | NodeLabel", node_id: str) -> Optional[GraphNode]:
        """One node by business key, or None."""
        label = validate_label(label)
        res = self._execute(q.find_node(label), {"id": node_id}, op=f"find_node:{label}")
        return GraphNode.from_record(res.records[0]["n"]) if res.records else None

    def find_nodes_by_property(self, label: "str | NodeLabel", prop: str, value: Any,
                               limit: int = 100) -> list[GraphNode]:
        """Nodes whose `prop` equals `value`. `prop` is validated as an identifier."""
        label = validate_label(label)
        res = self._execute(q.find_nodes_by_property(label, prop),
                            {"value": value, "limit": int(limit)},
                            op=f"find_nodes_by_property:{label}.{prop}")
        return [GraphNode.from_record(r["n"]) for r in res.records]

    def count_nodes(self, label: "str | NodeLabel") -> int:
        label = validate_label(label)
        res = self._execute(q.count_nodes(label), op=f"count_nodes:{label}")
        return res.records[0]["count"] if res.records else 0

    # ── relationship writes ──────────────────────────────────────────────────

    def create_relationship(self, start_label: "str | NodeLabel", start_id: str,
                            rel_type: "str | RelType",
                            end_label: "str | NodeLabel", end_id: str,
                            properties: dict[str, Any] | None = None) -> Optional[GraphRelationship]:
        """Create an edge, allowing parallel edges of the same type.
        Returns None when either endpoint does not exist (the MATCH finds nothing)."""
        start_label, end_label = validate_label(start_label), validate_label(end_label)
        rel_type = validate_rel_type(rel_type)
        res = self._execute(
            q.create_relationship(start_label, end_label, rel_type),
            {"start_id": start_id, "end_id": end_id, "props": properties or {}},
            op=f"create_rel:{start_label}-{rel_type}->{end_label}")
        if not res.records:
            log.warning("create_relationship matched no endpoints: (%s %s)-[%s]->(%s %s)",
                        start_label, start_id, rel_type, end_label, end_id)
            return None
        rec = res.records[0]
        return GraphRelationship(type=rel_type, start_id=rec["start_id"],
                                 end_id=rec["end_id"], properties=dict(rec["r"]))

    def merge_relationship(self, start_label: "str | NodeLabel", start_id: str,
                           rel_type: "str | RelType",
                           end_label: "str | NodeLabel", end_id: str,
                           properties: dict[str, Any] | None = None) -> Optional[GraphRelationship]:
        """Upsert an edge — at most one of this type between the two nodes."""
        return self.merge_relationship_with_stats(
            start_label, start_id, rel_type, end_label, end_id, properties)[0]

    def merge_relationship_with_stats(
            self, start_label: "str | NodeLabel", start_id: str,
            rel_type: "str | RelType",
            end_label: "str | NodeLabel", end_id: str,
            properties: dict[str, Any] | None = None
    ) -> "tuple[Optional[GraphRelationship], QueryStats]":
        """`merge_relationship` plus the write counters (created vs matched)."""
        start_label, end_label = validate_label(start_label), validate_label(end_label)
        rel_type = validate_rel_type(rel_type)
        res = self._execute(
            q.merge_relationship(start_label, end_label, rel_type),
            {"start_id": start_id, "end_id": end_id, "props": properties or {}},
            op=f"merge_rel:{start_label}-{rel_type}->{end_label}")
        stats = self._stats(res)
        if not res.records:
            log.warning("merge_relationship matched no endpoints: (%s %s)-[%s]->(%s %s)",
                        start_label, start_id, rel_type, end_label, end_id)
            return None, stats
        rec = res.records[0]
        return GraphRelationship(type=rel_type, start_id=rec["start_id"],
                                 end_id=rec["end_id"], properties=dict(rec["r"])), stats

    def delete_relationship(self, start_label: "str | NodeLabel", start_id: str,
                            rel_type: "str | RelType",
                            end_label: "str | NodeLabel", end_id: str) -> QueryStats:
        """Delete edges of one type between two nodes. Both nodes survive."""
        start_label, end_label = validate_label(start_label), validate_label(end_label)
        rel_type = validate_rel_type(rel_type)
        res = self._execute(
            q.delete_relationship(start_label, end_label, rel_type),
            {"start_id": start_id, "end_id": end_id},
            op=f"delete_rel:{start_label}-{rel_type}->{end_label}")
        return self._stats(res)

    # ── traversal ────────────────────────────────────────────────────────────

    def find_neighbors(self, label: "str | NodeLabel", node_id: str,
                       rel_type: "str | RelType | None" = None,
                       limit: int = 100) -> list[Neighbor]:
        """One hop in either direction. `rel_type=None` traverses every type."""
        label = validate_label(label)
        rel = validate_rel_type(rel_type) if rel_type else None
        res = self._execute(q.find_neighbors(label, rel), {"id": node_id, "limit": int(limit)},
                            op=f"find_neighbors:{label}" + (f":{rel}" if rel else ""))
        return [
            Neighbor(node=GraphNode.from_record(r["m"]), rel_type=r["rel_type"],
                     direction=r["direction"], rel_properties=dict(r["rel_props"] or {}))
            for r in res.records
        ]

    # ── Batch writes (phase 2.5) ─────────────────────────────────────────────
    # Added, never altered: every method above keeps its exact signature and
    # behaviour, so phase-1 and phase-2 callers are unaffected. These exist
    # because the knowledge-graph builder must not execute Cypher itself, and
    # writing a document one MERGE at a time is N round trips.

    def batch_merge_nodes(self, labels: "list[str]", rows: "list[dict[str, Any]]") -> QueryStats:
        """Upsert many nodes sharing one label-set in ONE round trip.

        `rows` must already carry id / props / aliases / provenance parameters —
        building those is the builder's job, executing Cypher is ours."""
        if not rows:
            return QueryStats()
        res = self._execute(q.batch_merge_nodes(labels), {"rows": rows},
                            op=f"batch_merge_nodes:{'+'.join(labels) or 'Entity'}[{len(rows)}]")
        return self._stats(res)

    def batch_merge_relationships(self, rel_type: "str | RelType",
                                  rows: "list[dict[str, Any]]") -> QueryStats:
        """Upsert many relationships of ONE type in ONE round trip."""
        if not rows:
            return QueryStats()
        rel_type = validate_rel_type(rel_type)
        res = self._execute(q.batch_merge_relationships(rel_type), {"rows": rows},
                            op=f"batch_merge_rels:{rel_type}[{len(rows)}]")
        return self._stats(res)

    def find_entity(self, entity_id: str) -> Optional["tuple[GraphNode, list[str]]"]:
        """Fetch a knowledge-graph node by id regardless of its specific labels.

        Returns (node, all_labels) — the multi-label model means `labels` is the
        interesting part, so it is returned rather than folded into GraphNode."""
        res = self._execute(q.find_entity_by_id(), {"id": entity_id}, op="find_entity")
        if not res.records:
            return None
        rec = res.records[0]
        return GraphNode.from_record(rec["n"]), list(rec["labels"])

    def find_entity_by_alias(self, name: str) -> Optional["tuple[GraphNode, list[str]]"]:
        """Resolve by canonical name or any recorded alias, case-insensitively."""
        res = self._execute(q.find_entity_by_alias(), {"name": name}, op="find_entity_by_alias")
        if not res.records:
            return None
        rec = res.records[0]
        return GraphNode.from_record(rec["n"]), list(rec["labels"])

    # ── Retrieval reads (phase 3) ────────────────────────────────────────────
    # Read-only additions. Cypher stays here because this class is the only
    # executor; the retrieval layer above composes these, it never writes Cypher.

    def fulltext_search(self, lucene_query: str, limit: int = 20) -> "list[dict[str, Any]]":
        """Fuzzy name/alias lookup. `lucene_query` must already be escaped."""
        res = self._execute(q.fulltext_search(),
                            {"index": q.FULLTEXT_INDEX, "query": lucene_query,
                             "limit": int(limit)},
                            op="fulltext_search")
        return [{"node": GraphNode.from_record(r["node"]),
                 "labels": list(r["labels"]), "score": float(r["score"])}
                for r in res.records]

    def find_entities_by_name(self, name: str, limit: int = 5
                              ) -> "list[tuple[GraphNode, list[str]]]":
        """Exact (case-insensitive) matches on canonical name or alias."""
        res = self._execute(q.find_entities_by_exact_name(),
                            {"name": name, "limit": int(limit)},
                            op="find_entities_by_name")
        return [(GraphNode.from_record(r["n"]), list(r["labels"])) for r in res.records]

    def fetch_entities(self, ids: "list[str]") -> "list[dict[str, Any]]":
        """Load nodes by id together with their degree (importance signal)."""
        if not ids:
            return []
        res = self._execute(q.fetch_entities(), {"ids": list(ids)}, op="fetch_entities")
        return [{"node": GraphNode.from_record(r["n"]), "labels": list(r["labels"]),
                 "degree": int(r["degree"])} for r in res.records]

    def expand_one_hop(self, ids: "list[str]", visited: "list[str]",
                       limit: int = 500) -> "list[dict[str, Any]]":
        """One hop out from `ids`, skipping anything already in `visited`."""
        if not ids:
            return []
        res = self._execute(q.expand_one_hop(),
                            {"ids": list(ids), "visited": list(visited), "limit": int(limit)},
                            op=f"expand_one_hop[{len(ids)}]")
        return [{"from_id": r["from_id"], "node": GraphNode.from_record(r["node"]),
                 "labels": list(r["labels"]), "rel_type": r["rel_type"],
                 "start_id": r["start_id"], "end_id": r["end_id"],
                 "rel_props": dict(r["rel_props"] or {}), "degree": int(r["degree"])}
                for r in res.records]

    def edges_between(self, ids: "list[str]") -> "list[dict[str, Any]]":
        """Every edge whose endpoints are both in `ids` — the induced subgraph."""
        if not ids:
            return []
        res = self._execute(q.edges_between(), {"ids": list(ids)}, op="edges_between")
        return [{"rel_type": r["rel_type"], "start_id": r["start_id"],
                 "end_id": r["end_id"], "rel_props": dict(r["rel_props"] or {})}
                for r in res.records]

    def add_aliases(self, entity_id: str, aliases: "list[str]") -> "list[str]":
        """Append aliases to a node (deduplicated). Returns the resulting list."""
        if not aliases:
            return []
        res = self._execute(q.add_alias_to_entity(),
                            {"id": entity_id, "aliases": list(aliases)}, op="add_aliases")
        return list(res.records[0]["aliases"]) if res.records else []

    def all_alias_pairs(self) -> "list[dict[str, Any]]":
        """Every canonical_name/aliases row — used to warm the registry cache."""
        res = self._execute(q.all_alias_pairs(), op="all_alias_pairs")
        return [{"id": r["id"], "canonical_name": r["canonical_name"],
                 "aliases": list(r["aliases"] or [])} for r in res.records]

    def find_legacy_scheme_nodes(self) -> "list[dict[str, Any]]":
        """Phase-2 nodes still using the `label:slug` id scheme."""
        res = self._execute(q.find_legacy_scheme_nodes(), op="find_legacy_nodes")
        return [{"id": r["id"], "canonical_name": r["canonical_name"],
                 "name": r["name"], "labels": list(r["labels"])} for r in res.records]

    # ── escape hatch ─────────────────────────────────────────────────────────

    def run_query(self, cypher: str, params: dict[str, Any] | None = None,
                  *, op: str = "raw") -> list[dict[str, Any]]:
        """Run an arbitrary parameterized statement and return plain dicts.

        For reads the typed helpers don't cover. Callers MUST parameterize values
        — passing an f-string built from user input reintroduces exactly the
        injection risk this package exists to prevent."""
        res = self._execute(cypher, params, op=op)
        return [r.data() for r in res.records]

    # ── health ───────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """True when the graph answers a trivial query."""
        try:
            res = self._execute(q.PING, op="ping")
            return bool(res.records and res.records[0]["ok"] == 1)
        except (GraphUnavailable, Neo4jError):
            return False


_default: Optional[GraphService] = None


def get_graph_service() -> GraphService:
    """FastAPI-dependency-friendly accessor for the shared instance.

    Constructing this does NOT connect — the driver resolves on first query — so
    it is safe to depend on even when Neo4j is down.
    """
    global _default
    if _default is None:
        _default = GraphService()
    return _default
