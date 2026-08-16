"""
KnowledgeGraphBuilder — extracted entities and relationships in, graph writes out.

The builder executes NO Cypher. It calls GraphService and nothing else; that is
what keeps every statement in one reviewed place (queries.py) and every write
timed and validated the same way.

Idempotency comes from the node id, not from checking-before-writing. Each entity
gets a deterministic id derived from its canonical name, so processing the same
conversation twice MERGEs onto the same nodes instead of creating duplicates. The
uniqueness constraint bootstrap creates for every label is what enforces that at
the database level rather than on trust.
"""
from __future__ import annotations

import logging
import re
import os
import time
import unicodedata
from typing import Iterable, Optional

from .models import NodeLabel, RelType
from .provenance import Provenance
from .service import GraphService, get_graph_service
from .types import BuildResult, ExtractedEntity, ExtractedRelationship

#: The node/relationship property carrying the tenant. Read from the SAME env
#: var the read side uses (backend/orchestrator/graph_tools.py) so a writer and a
#: filter can never disagree about which property they mean.
TENANT_PROPERTY = os.getenv("GRAPH_TENANT_PROPERTY", "org_id")

#: Sentinel for "not tenant-scoped". Mirrors backend/ingest.py's SHARED_TENANT.
SHARED_TENANT = "__shared__"

log = logging.getLogger("aganeti.kg.builder")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_VALID_LABELS = {label.value for label in NodeLabel}
_VALID_REL_TYPES = {rel.value for rel in RelType}


def slugify(name: str) -> str:
    """Canonical name → stable id fragment.

    Deterministic and lossy by design: "Microsoft Graph", "microsoft graph" and
    "Microsoft  Graph" all yield `microsoft-graph`, which is exactly what makes
    re-processing the same text idempotent. Unicode is folded to ASCII first so
    accented spellings do not fork the node.
    """
    if not name:
        return ""
    ascii_name = (unicodedata.normalize("NFKD", str(name))
                  .encode("ascii", "ignore").decode("ascii"))
    return _SLUG_STRIP.sub("-", ascii_name.lower()).strip("-")


def node_id_for(label: str, name: str) -> str:
    """The id a given (label, name) always maps to.

    Prefixing with the label keeps ids readable in the Neo4j browser and stops a
    Person and a Project of the same name colliding conceptually — the
    uniqueness constraint is per-label, but a shared id would still be confusing.
    """
    return f"{label.lower()}:{slugify(name)}"


class KnowledgeGraphBuilder:
    """Persists an extraction result. Idempotent; calls only GraphService."""

    def __init__(self, service: Optional[GraphService] = None,
                 source: str = "conversation",
                 tenant_id: str | None = None) -> None:
        """`source` is stamped on every node/edge written, so graph content can
        later be attributed (or removed) by origin.

        `tenant_id` follows BatchKnowledgeGraphBuilder EXACTLY — same parameter,
        same `None -> SHARED_TENANT` default, same module constants. This class is
        the legacy single-write builder, reachable only via
        `KnowledgeGraphPipeline(legacy_builder=True)`, which nothing in the tree
        sets; but "unreachable" is a property of today's call sites, not of the
        code, and it was verified to write org_id=None. A writer that produces an
        unstamped node is one constructor call away from re-diluting whatever a
        future backfill cleans, so it stamps too rather than being trusted to stay
        unused.
        """
        self._svc = service or get_graph_service()
        self._source = source
        self._tenant_id = tenant_id or SHARED_TENANT

    # ── nodes ────────────────────────────────────────────────────────────────

    def _write_entity(self, entity: ExtractedEntity, result: BuildResult) -> Optional[str]:
        """MERGE one entity. Returns its node id, or None if it was skipped."""
        if entity.type not in _VALID_LABELS:
            result.skipped_entities.append(f"{entity.name} (unknown label {entity.type})")
            return None
        node_id = node_id_for(entity.type, entity.name)
        if not node_id or node_id.endswith(":"):
            # A name that slugifies to nothing (punctuation/emoji only) would
            # otherwise create a node with an unusable, colliding id.
            result.skipped_entities.append(f"{entity.name} (empty id after slugify)")
            return None

        props = {
            "name": entity.name,
            "type": entity.type,
            "source": self._source,
            **entity.properties,
            # LAST, so an extractor-supplied `org_id` cannot set its own
            # visibility. Same rule and same position as the batch builder.
            TENANT_PROPERTY: self._tenant_id,
        }
        try:
            _, stats = self._svc.merge_node_with_stats(entity.type, node_id, props)
        except Exception as e:  # noqa: BLE001 — one bad entity must not fail the batch
            log.warning("kg: failed to merge %s %r: %s", entity.type, entity.name, e)
            result.skipped_entities.append(f"{entity.name} ({type(e).__name__})")
            return None

        if stats.nodes_created:
            result.nodes_created += 1
        else:
            result.nodes_merged += 1
        entity.node_id = node_id
        return node_id

    # ── relationships ────────────────────────────────────────────────────────

    def _write_relationship(self, rel: ExtractedRelationship,
                            index: dict[str, ExtractedEntity],
                            result: BuildResult) -> None:
        """MERGE one edge, resolving endpoints through the entity index."""
        if rel.type not in _VALID_REL_TYPES:
            result.skipped_relationships.append(f"{rel.source}-{rel.type}->{rel.target} "
                                                "(unknown type)")
            return
        source = index.get(rel.source.lower())
        target = index.get(rel.target.lower())
        if source is None or target is None:
            # The model named something it never extracted. Writing it would mean
            # inventing a node with no type, so the edge is dropped and reported.
            missing = rel.source if source is None else rel.target
            result.skipped_relationships.append(
                f"{rel.source}-{rel.type}->{rel.target} (unknown entity {missing!r})")
            return
        if not source.node_id or not target.node_id:
            result.skipped_relationships.append(
                f"{rel.source}-{rel.type}->{rel.target} (endpoint not persisted)")
            return

        props = {"source": self._source, **rel.properties,
                 # Tenant last, never overridable — as for nodes.
                 TENANT_PROPERTY: self._tenant_id}
        try:
            _, stats = self._svc.merge_relationship_with_stats(
                source.type, source.node_id, rel.type, target.type, target.node_id, props)
        except Exception as e:  # noqa: BLE001
            log.warning("kg: failed to merge %s-[%s]->%s: %s",
                        rel.source, rel.type, rel.target, e)
            result.skipped_relationships.append(
                f"{rel.source}-{rel.type}->{rel.target} ({type(e).__name__})")
            return

        if stats.relationships_created:
            result.relationships_created += 1
        else:
            result.relationships_merged += 1

    # ── entry point ──────────────────────────────────────────────────────────

    def build(self, entities: Iterable[ExtractedEntity],
              relationships: Iterable[ExtractedRelationship] | None = None) -> BuildResult:
        """Write entities then relationships. Safe to re-run on the same input.

        Nodes go first because an edge cannot MERGE onto an endpoint that does
        not exist yet — GraphService's MATCH would find nothing and the edge
        would be silently dropped.
        """
        started = time.perf_counter()
        result = BuildResult()
        entities = list(entities)

        index: dict[str, ExtractedEntity] = {}
        for entity in entities:
            if self._write_entity(entity, result) is not None:
                index[entity.name.lower()] = entity

        for rel in (relationships or []):
            self._write_relationship(rel, index, result)

        result.took_ms = (time.perf_counter() - started) * 1000
        log.info("kg build: %d nodes (+%d new), %d edges (+%d new), "
                 "%d entities skipped, %d edges skipped, %.0fms",
                 result.nodes_created + result.nodes_merged, result.nodes_created,
                 result.relationships_created + result.relationships_merged,
                 result.relationships_created,
                 len(result.skipped_entities), len(result.skipped_relationships),
                 result.took_ms)
        return result


# ── Phase 2.5: batched, multi-label, provenance-carrying writes ──────────────

def entity_id_for(canonical_name: str) -> str:
    """The id a canonical name always maps to — LABEL-FREE, unlike phase 2.

    Phase 2 used `{label}:{slug}`, which forked "Microsoft Graph" into
    `technology:microsoft-graph` and `api:microsoft-graph` the moment the model
    changed its mind about the label. Identity is the *thing*, not its
    classification, so the id is now the slug alone and labels accumulate on the
    single node instead.
    """
    return slugify(canonical_name)


class BatchKnowledgeGraphBuilder:
    """Writes a whole extraction in a handful of round trips.

    Grouping strategy — Cypher cannot parameterize a label or a relationship
    type, and APOC (which offers dynamic labels) is not installed here, so rows
    are grouped by label-set and by relationship type. One document typically
    yields 3–8 queries in total regardless of how many nodes it contains, versus
    one query per node and per edge in phase 2.

    Still calls only GraphService. Still idempotent: ids are deterministic and
    every write is a MERGE.
    """

    def __init__(self, service: Optional[GraphService] = None,
                 source: str = "conversation", batch_size: int = 500,
                 tenant_id: str | None = None) -> None:
        self._svc = service or get_graph_service()
        self._source = source
        # STAMPED AT WRITE TIME, ahead of any backfill.
        #
        # Ordering matters and it is the same lesson as the Qdrant side: an
        # unstamped ingestion path re-dilutes whatever a backfill just cleaned,
        # so the writer has to stamp BEFORE the backfill runs, not after. A
        # backfill against a still-unstamped writer is a treadmill.
        #
        # `None` means "not tenant-scoped" and writes SHARED_TENANT, matching
        # backend/ingest.py's convention exactly: unstamped/shared data stays
        # visible in lenient mode and is explicit rather than absent, so the
        # strict ratchet can distinguish "shared on purpose" from "never
        # stamped". Two stores, one convention.
        self._tenant_id = tenant_id or SHARED_TENANT
        # Neo4j holds the whole UNWIND list in memory for the transaction; very
        # large batches trade round trips for heap pressure and lock duration.
        self._batch_size = max(1, batch_size)

    # ── row construction ─────────────────────────────────────────────────────

    @staticmethod
    def _chunks(rows: list, size: int):
        for i in range(0, len(rows), size):
            yield rows[i:i + size]

    def _entity_row(self, entity: ExtractedEntity, prov: dict) -> Optional[dict]:
        node_id = entity_id_for(entity.canonical_name or entity.name)
        if not node_id:
            return None
        props = {
            "canonical_name": entity.canonical_name or entity.name,
            "name": entity.canonical_name or entity.name,
            "primary_label": entity.type,
            "secondary_labels": list(entity.secondary_labels),
            "source": self._source,
            **entity.properties,
            # Written LAST so extractor-supplied properties can never overwrite
            # the tenant — an entity's own `org_id` field would otherwise decide
            # its visibility.
            TENANT_PROPERTY: self._tenant_id,
        }
        entity.node_id = node_id
        return {"id": node_id, "props": props,
                "aliases": sorted({a for a in entity.aliases if a}), **prov}

    def _relationship_row(self, rel: ExtractedRelationship,
                          index: dict[str, ExtractedEntity], prov: dict) -> Optional[dict]:
        source = index.get(rel.source.lower())
        target = index.get(rel.target.lower())
        if source is None or target is None or not source.node_id or not target.node_id:
            return None
        row = {"start_id": source.node_id, "end_id": target.node_id,
               "props": {"source": self._source, **rel.properties,
                         # Same rule as nodes: tenant last, never overridable.
                         TENANT_PROPERTY: self._tenant_id}, **prov}
        # Per-edge confidence overrides the document-level default when stated.
        row["confidence"] = rel.confidence if rel.confidence is not None else prov["confidence"]
        return row

    # ── entry point ──────────────────────────────────────────────────────────

    def build(self, entities: Iterable[ExtractedEntity],
              relationships: Iterable[ExtractedRelationship] | None = None,
              provenance: Optional[Provenance] = None) -> BuildResult:
        """Persist an extraction. Same contract as the phase-2 builder, batched."""
        started = time.perf_counter()
        result = BuildResult()
        entities = list(entities)
        prov = (provenance or Provenance(source_type=self._source)).as_params()

        # ── nodes, grouped by label-set ──────────────────────────────────────
        groups: dict[tuple[str, ...], list[dict]] = {}
        index: dict[str, ExtractedEntity] = {}
        for entity in entities:
            labels = tuple(l for l in entity.all_labels if l in _VALID_LABELS)
            if not labels:
                result.skipped_entities.append(f"{entity.name} (unknown label {entity.type})")
                continue
            # Per-entity confidence wins when the extractor stated one; otherwise
            # the entity inherits the document-level confidence from provenance.
            conf = entity.confidence if entity.confidence is not None else prov["confidence"]
            row = self._entity_row(entity, dict(prov, confidence=conf))
            if row is None:
                result.skipped_entities.append(f"{entity.name} (empty id after slugify)")
                continue
            groups.setdefault(labels, []).append(row)
            index[entity.name.lower()] = entity
            for alias in entity.aliases:
                index.setdefault(alias.lower(), entity)

        for labels, rows in groups.items():
            for chunk in self._chunks(rows, self._batch_size):
                try:
                    stats = self._svc.batch_merge_nodes(list(labels), chunk)
                except Exception as e:  # noqa: BLE001 — one bad group must not lose the rest
                    log.warning("kg: node batch %s failed: %s", "+".join(labels), e)
                    result.skipped_entities += [r["id"] for r in chunk]
                    continue
                result.graph_queries += 1
                result.nodes_created += stats.nodes_created
                result.nodes_merged += len(chunk) - stats.nodes_created

        # ── relationships, grouped by type ───────────────────────────────────
        rel_groups: dict[str, list[dict]] = {}
        for rel in (relationships or []):
            if rel.type not in _VALID_REL_TYPES:
                result.skipped_relationships.append(
                    f"{rel.source}-{rel.type}->{rel.target} (unknown type)")
                continue
            row = self._relationship_row(rel, index, dict(prov))
            if row is None:
                missing = rel.source if rel.source.lower() not in index else rel.target
                result.skipped_relationships.append(
                    f"{rel.source}-{rel.type}->{rel.target} (unknown entity {missing!r})")
                continue
            rel_groups.setdefault(rel.type, []).append(row)

        for rel_type, rows in rel_groups.items():
            for chunk in self._chunks(rows, self._batch_size):
                try:
                    stats = self._svc.batch_merge_relationships(rel_type, chunk)
                except Exception as e:  # noqa: BLE001
                    log.warning("kg: relationship batch %s failed: %s", rel_type, e)
                    result.skipped_relationships += [
                        f"{r['start_id']}-{rel_type}->{r['end_id']}" for r in chunk]
                    continue
                result.graph_queries += 1
                result.relationships_created += stats.relationships_created
                result.relationships_merged += len(chunk) - stats.relationships_created

        result.took_ms = (time.perf_counter() - started) * 1000
        log.info("kg batch build: %d nodes (+%d new) in %d group(s), "
                 "%d edges (+%d new), %d query/queries total, %.0fms",
                 result.nodes_created + result.nodes_merged, result.nodes_created,
                 len(groups), result.relationships_created + result.relationships_merged,
                 result.relationships_created, result.graph_queries, result.took_ms)
        return result
