"""
Cypher statements, kept out of the service body so they can be read and reviewed
as a set.

Rules for everything in this file:

  * Every VALUE is a parameter — `$id`, `$props`, `$limit`. No f-string ever
    carries user data into a query.
  * The only interpolation is the label / relationship type, which Cypher cannot
    parameterize. Those come from `{label}` / `{rel_type}` placeholders filled
    ONLY with strings that passed models.validate_label / validate_rel_type.
    Templates are exposed as builder functions rather than bare strings so
    nobody can `.format()` one with an unvalidated value by accident.
  * `SET n += $props` merges properties rather than replacing the node, so a
    merge never silently drops fields written by an earlier call.
"""
from __future__ import annotations

from .models import validate_label, validate_rel_type

# ── Node reads ────────────────────────────────────────────────────────────────

def find_node(label: str) -> str:
    """Fetch one node by business key."""
    return f"""
    MATCH (n:{validate_label(label)} {{id: $id}})
    RETURN n
    """


def find_nodes_by_property(label: str, prop: str) -> str:
    """Fetch nodes whose `prop` equals $value. `prop` is validated as an identifier."""
    return f"""
    MATCH (n:{validate_label(label)})
    WHERE n.{validate_label(prop)} = $value
    RETURN n
    ORDER BY n.id
    LIMIT $limit
    """


# ── Node writes ───────────────────────────────────────────────────────────────

def create_node(label: str) -> str:
    """CREATE — fails on a duplicate id once the uniqueness constraint exists.
    Use when a duplicate genuinely is an error; otherwise prefer merge_node."""
    return f"""
    CREATE (n:{validate_label(label)})
    SET n += $props, n.id = $id, n.created_at = datetime(), n.updated_at = datetime()
    RETURN n
    """


def merge_node(label: str) -> str:
    """Idempotent upsert on the business key. `ON CREATE` stamps created_at once
    so it survives later merges; `+=` keeps properties this call didn't mention."""
    return f"""
    MERGE (n:{validate_label(label)} {{id: $id}})
    ON CREATE SET n.created_at = datetime()
    SET n += $props, n.updated_at = datetime()
    RETURN n
    """


def delete_node(label: str) -> str:
    """DETACH DELETE — removes the node and any relationships attached to it.
    Without DETACH, Neo4j refuses to delete a connected node."""
    return f"""
    MATCH (n:{validate_label(label)} {{id: $id}})
    DETACH DELETE n
    """


# ── Relationship writes ───────────────────────────────────────────────────────

def create_relationship(start_label: str, end_label: str, rel_type: str) -> str:
    """CREATE a relationship — permits parallel edges of the same type."""
    return f"""
    MATCH (a:{validate_label(start_label)} {{id: $start_id}})
    MATCH (b:{validate_label(end_label)} {{id: $end_id}})
    CREATE (a)-[r:{validate_rel_type(rel_type)}]->(b)
    SET r += $props, r.created_at = datetime()
    RETURN r, a.id AS start_id, b.id AS end_id
    """


def merge_relationship(start_label: str, end_label: str, rel_type: str) -> str:
    """Idempotent edge: at most one of this type between the two nodes."""
    return f"""
    MATCH (a:{validate_label(start_label)} {{id: $start_id}})
    MATCH (b:{validate_label(end_label)} {{id: $end_id}})
    MERGE (a)-[r:{validate_rel_type(rel_type)}]->(b)
    ON CREATE SET r.created_at = datetime()
    SET r += $props, r.updated_at = datetime()
    RETURN r, a.id AS start_id, b.id AS end_id
    """


def delete_relationship(start_label: str, end_label: str, rel_type: str) -> str:
    """Delete edges of one type between two nodes; leaves both nodes intact."""
    return f"""
    MATCH (a:{validate_label(start_label)} {{id: $start_id}})
          -[r:{validate_rel_type(rel_type)}]->
          (b:{validate_label(end_label)} {{id: $end_id}})
    DELETE r
    """


# ── Traversal ─────────────────────────────────────────────────────────────────

def find_neighbors(label: str, rel_type: "str | None" = None) -> str:
    """One hop in either direction from a node.

    `rel_type=None` traverses every type. Direction is derived in Cypher from
    which end the anchor sits on, so callers get "out"/"in" without a second query.
    """
    rel = f":{validate_rel_type(rel_type)}" if rel_type else ""
    return f"""
    MATCH (n:{validate_label(label)} {{id: $id}})-[r{rel}]-(m)
    RETURN m,
           type(r) AS rel_type,
           CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END AS direction,
           properties(r) AS rel_props
    ORDER BY rel_type, m.id
    LIMIT $limit
    """


def count_nodes(label: str) -> str:
    return f"MATCH (n:{validate_label(label)}) RETURN count(n) AS count"


# ── Introspection (used by bootstrap + health) ────────────────────────────────

SHOW_CONSTRAINTS = "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type"
SHOW_INDEXES = "SHOW INDEXES YIELD name, labelsOrTypes, properties, state, type"
PING = "RETURN 1 AS ok"


# ── Batch writes (phase 2.5) ─────────────────────────────────────────────────
# Everything above is one-statement-per-entity. These take an `$rows` list and do
# the whole batch in a single round trip.
#
# Two things Cypher will not let us parameterize: node LABELS and relationship
# TYPES. Without APOC (not installed here) there is no per-row dynamic label, so
# the builder GROUPS rows by label-set / relationship type and calls these once
# per distinct group — typically 3–8 queries for a whole document, instead of one
# per node. `BASE_LABEL` is what makes that work: every knowledge-graph node also
# carries `:Entity`, so a MERGE and every endpoint MATCH can use one indexed
# label regardless of what else the node is.

BASE_LABEL = "Entity"


def _provenance_set_clause(var: str = "n") -> str:
    """SET fragment implementing the accumulate-don't-overwrite rules.

    - list fields append only values not already present (set-union semantics)
    - first_seen is ON CREATE only, so the original sighting survives
    - confidence keeps the MAXIMUM ever seen, not the latest
    - observations counts every sighting
    """
    unions = "\n".join(
        f"        {var}.{fld} = [x IN coalesce({var}.{fld}, []) WHERE NOT x IN row.add_{fld}]"
        f" + row.add_{fld},"
        for fld in ("source_ids", "source_types", "user_ids",
                    "conversation_ids", "message_ids", "document_ids", "models")
    )
    return f"""
    SET {unions}
        {var}.last_seen = row.last_seen,
        {var}.observations = coalesce({var}.observations, 0) + 1,
        {var}.confidence = CASE
            WHEN coalesce({var}.confidence, 0.0) > row.confidence
            THEN {var}.confidence ELSE row.confidence END,
        {var}.last_source_id = row.source_id,
        {var}.last_source_type = row.source_type,
        {var}.last_model = row.model"""


def batch_merge_nodes(labels: "list[str]") -> str:
    """Upsert many nodes that share one label-set, in a single round trip.

    MERGE is on (:Entity {id}) — never on the specific label — so a node first
    seen as :Technology and later as :API gains a label instead of forking into
    a second node. That is the whole fix for the phase-2 duplicate-id problem.
    """
    extra = [validate_label(l) for l in labels if validate_label(l) != BASE_LABEL]
    # `ON CREATE SET` must follow MERGE immediately, so the label SET goes after it.
    label_clause = f"\n    SET n:{':'.join(extra)}" if extra else ""
    return f"""
    UNWIND $rows AS row
    MERGE (n:{BASE_LABEL} {{id: row.id}})
    ON CREATE SET n.first_seen = row.first_seen, n.created_at = datetime(){label_clause}
    SET n += row.props,
        n.updated_at = datetime(),
        n.aliases = [x IN coalesce(n.aliases, []) WHERE NOT x IN row.aliases] + row.aliases
    {_provenance_set_clause("n")}
    RETURN count(n) AS touched
    """


def batch_merge_relationships(rel_type: str) -> str:
    """Upsert many relationships of ONE type in a single round trip.

    Endpoints are matched on the shared base label so this works no matter what
    specific labels either end carries."""
    return f"""
    UNWIND $rows AS row
    MATCH (a:{BASE_LABEL} {{id: row.start_id}})
    MATCH (b:{BASE_LABEL} {{id: row.end_id}})
    MERGE (a)-[r:{validate_rel_type(rel_type)}]->(b)
    ON CREATE SET r.first_seen = row.first_seen, r.created_at = datetime()
    SET r += row.props, r.updated_at = datetime()
    {_provenance_set_clause("r")}
    RETURN count(r) AS touched
    """


def find_entity_by_id() -> str:
    """Fetch a knowledge-graph node by id, whatever its specific labels are."""
    return f"""
    MATCH (n:{BASE_LABEL} {{id: $id}})
    RETURN n, labels(n) AS labels
    """


def find_entity_by_alias() -> str:
    """Resolve a node by canonical name OR any recorded alias (case-insensitive)."""
    return f"""
    MATCH (n:{BASE_LABEL})
    WHERE toLower(n.canonical_name) = toLower($name)
       OR any(a IN coalesce(n.aliases, []) WHERE toLower(a) = toLower($name))
    RETURN n, labels(n) AS labels
    LIMIT 1
    """


# ── Retrieval (phase 3) ──────────────────────────────────────────────────────
# Read-only. Nothing below writes, and nothing below builds a prompt or an
# answer — this layer's whole job is to hand back a ranked, evidence-carrying
# subgraph that some later phase may choose to render.

FULLTEXT_INDEX = "kg_entity_search"

# Indexing `aliases` as well as `canonical_name` is what makes "MS Graph" find
# the Microsoft Graph node: Neo4j indexes array properties element-wise.
CREATE_FULLTEXT_INDEX = (
    f"CREATE FULLTEXT INDEX {FULLTEXT_INDEX} IF NOT EXISTS "
    f"FOR (n:{BASE_LABEL}) ON EACH [n.canonical_name, n.aliases]"
)


def fulltext_search() -> str:
    """Fuzzy lookup by name or alias, scored by Lucene.

    `$query` is a Lucene expression built by the registry (never raw user text —
    unescaped Lucene syntax would either error or silently mean something else).
    """
    return f"""
    CALL db.index.fulltext.queryNodes($index, $query, {{limit: $limit}})
    YIELD node, score
    WHERE node:{BASE_LABEL}
    RETURN node, labels(node) AS labels, score
    ORDER BY score DESC
    """


def find_entities_by_exact_name() -> str:
    """Exact (case-insensitive) match on canonical name or any alias."""
    return f"""
    MATCH (n:{BASE_LABEL})
    WHERE toLower(n.canonical_name) = toLower($name)
       OR any(a IN coalesce(n.aliases, []) WHERE toLower(a) = toLower($name))
    RETURN n, labels(n) AS labels
    ORDER BY coalesce(n.observations, 0) DESC
    LIMIT $limit
    """


def expand_one_hop() -> str:
    """One hop out from a frontier of ids, returning EVERY edge with its evidence.

    Expansion is done a hop at a time rather than with a variable-length pattern
    so that (a) the frontier can be de-duplicated between hops, (b) the per-hop
    fan-out is bounded, and (c) each edge's provenance comes back with it instead
    of being re-queried. Direction is reported relative to the frontier node.
    """
    return f"""
    UNWIND $ids AS seed_id
    MATCH (a:{BASE_LABEL} {{id: seed_id}})-[r]-(b:{BASE_LABEL})
    WHERE NOT b.id IN $visited
    RETURN a.id            AS from_id,
           b               AS node,
           labels(b)       AS labels,
           type(r)         AS rel_type,
           startNode(r).id AS start_id,
           endNode(r).id   AS end_id,
           properties(r)   AS rel_props,
           COUNT {{ (b)--() }} AS degree
    LIMIT $limit
    """


def fetch_entities() -> str:
    """Load a set of nodes by id, with the degree used for importance ranking."""
    return f"""
    MATCH (n:{BASE_LABEL}) WHERE n.id IN $ids
    RETURN n, labels(n) AS labels, COUNT {{ (n)--() }} AS degree
    """


def edges_between() -> str:
    """Every edge WITHIN a node set — the induced subgraph, with evidence.

    Run after expansion so the returned subgraph is closed: the caller gets the
    edges among the nodes it was given, not just the ones traversal happened to
    walk."""
    return f"""
    MATCH (a:{BASE_LABEL})-[r]->(b:{BASE_LABEL})
    WHERE a.id IN $ids AND b.id IN $ids
    RETURN type(r)       AS rel_type,
           a.id          AS start_id,
           b.id          AS end_id,
           properties(r) AS rel_props
    """


def add_alias_to_entity() -> str:
    """Append aliases to a node, de-duplicated. Used by the registry."""
    return f"""
    MATCH (n:{BASE_LABEL} {{id: $id}})
    SET n.aliases = [x IN coalesce(n.aliases, []) WHERE NOT x IN $aliases] + $aliases
    RETURN n.aliases AS aliases
    """


def all_alias_pairs() -> str:
    """Every (alias → canonical id) the graph knows. Warms the registry cache."""
    return f"""
    MATCH (n:{BASE_LABEL})
    WHERE size(coalesce(n.aliases, [])) > 0 OR n.canonical_name IS NOT NULL
    RETURN n.id AS id, n.canonical_name AS canonical_name,
           coalesce(n.aliases, []) AS aliases
    """


def find_legacy_scheme_nodes() -> str:
    """Phase-2 nodes whose id still carries a label prefix (`technology:foo`).

    These cannot merge with phase-2.5 writes, so the registry offers to fold
    them in. Matching on the literal ':' is enough — new ids are pure slugs."""
    return f"""
    MATCH (n:{BASE_LABEL})
    WHERE n.id CONTAINS ':'
    RETURN n.id AS id, n.canonical_name AS canonical_name, n.name AS name,
           labels(n) AS labels
    """
