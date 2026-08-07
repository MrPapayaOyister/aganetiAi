"""
Schema bootstrap — uniqueness constraints and indexes.

Every statement is `IF NOT EXISTS`, so this is safe to run on every boot and
safe to run against a populated database. **Nothing here deletes or rewrites
data**: constraints and indexes are metadata, and creating them leaves existing
nodes and relationships untouched.

One caveat worth knowing rather than discovering: a uniqueness constraint cannot
be created if the data already violates it. That surfaces as a
`ConstraintCreationFailed` error naming the offending label — we log it clearly
and carry on with the remaining statements instead of aborting the boot.

A uniqueness constraint also creates a backing index, so `User(id)` needs no
separate index; the extra indexes below are for the properties we expect to
filter on that are not the business key.
"""
from __future__ import annotations

import logging

from neo4j.exceptions import ClientError, Neo4jError

from .client import GraphUnavailable
from .models import NodeLabel
from .service import GraphService, get_graph_service

log = logging.getLogger("aganeti.graph.bootstrap")

# Every label gets an `id` uniqueness constraint: `id` is the business key for
# all of them, and the constraint is what makes MERGE-by-id both correct (no
# duplicate nodes when the builder re-runs) and fast (it creates a backing index).
# Derived from the enum so a new label is constrained automatically.
CONSTRAINED_LABELS: tuple[NodeLabel, ...] = tuple(NodeLabel)

# (label, property) pairs to index beyond the business key — lookup paths we
# expect (find a user by email, entities by name/type, documents by owner).
SECONDARY_INDEXES: tuple[tuple[NodeLabel, str], ...] = (
    (NodeLabel.USER, "email"),
    (NodeLabel.DOCUMENT, "owner_id"),
    (NodeLabel.CONVERSATION, "user_id"),
    # `name` is how the knowledge-graph builder looks entities up when an id
    # collision needs disambiguating, and how a human browses the graph.
    (NodeLabel.ENTITY, "name"),
    (NodeLabel.ENTITY, "type"),
    (NodeLabel.PERSON, "name"),
    (NodeLabel.PROJECT, "name"),
    (NodeLabel.TECHNOLOGY, "name"),
    (NodeLabel.ORGANIZATION, "name"),
    (NodeLabel.COMPANY, "name"),
    (NodeLabel.MODEL, "name"),
    (NodeLabel.PROVIDER, "name"),
    (NodeLabel.SERVICE, "name"),
)


def _constraint_name(label: str) -> str:
    return f"constraint_{label.lower()}_id_unique"


def _index_name(label: str, prop: str) -> str:
    return f"index_{label.lower()}_{prop.lower()}"


def constraint_statements() -> list[tuple[str, str]]:
    """(name, cypher) for each uniqueness constraint. Label comes from a closed enum."""
    out = []
    for label in CONSTRAINED_LABELS:
        name = _constraint_name(label.value)
        out.append((name,
                    f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                    f"FOR (n:{label.value}) REQUIRE n.id IS UNIQUE"))
    return out


def index_statements() -> list[tuple[str, str]]:
    """(name, cypher) for each secondary index."""
    out = []
    for label, prop in SECONDARY_INDEXES:
        name = _index_name(label.value, prop)
        out.append((name,
                    f"CREATE INDEX {name} IF NOT EXISTS "
                    f"FOR (n:{label.value}) ON (n.{prop})"))
    return out


def fulltext_statements() -> list[tuple[str, str]]:
    """The full-text index backing fuzzy entity lookup (phase 3).

    Separate from the property indexes because it is a different index TYPE and
    a different failure mode: without it, retrieval silently degrades to exact
    matching rather than erroring."""
    from . import queries as q
    return [(q.FULLTEXT_INDEX, q.CREATE_FULLTEXT_INDEX)]


def bootstrap_schema(service: GraphService | None = None) -> dict:
    """Create every constraint and index. Idempotent, additive, never destructive.

    Returns a summary dict; never raises for an individual statement failure, so
    one bad label cannot stop the rest of the schema (or the app) from coming up.
    """
    svc = service or get_graph_service()
    applied, failed = [], []

    for name, cypher in constraint_statements() + index_statements() + fulltext_statements():
        try:
            svc.run_query(cypher, op=f"bootstrap:{name}")
            applied.append(name)
        except ClientError as e:
            # EquivalentSchemaRuleAlreadyExists: an equivalent rule exists under a
            # different name (e.g. created by hand in the browser). Benign.
            if "EquivalentSchemaRuleAlreadyExists" in (e.code or ""):
                applied.append(name)
                continue
            log.warning("graph bootstrap: %s not applied — %s: %s", name, e.code, e.message)
            failed.append(name)
        except (GraphUnavailable, Neo4jError) as e:
            log.warning("graph bootstrap: %s not applied — %s", name, e)
            failed.append(name)

    log.info("graph bootstrap: %d/%d schema objects in place%s",
             len(applied), len(applied) + len(failed),
             f" ({len(failed)} failed: {', '.join(failed)})" if failed else "")
    return {"applied": applied, "failed": failed}


def describe_schema(service: GraphService | None = None) -> dict:
    """Read back what actually exists — used by the verification script."""
    from . import queries as q
    svc = service or get_graph_service()
    return {
        "constraints": svc.run_query(q.SHOW_CONSTRAINTS, op="show_constraints"),
        "indexes": svc.run_query(q.SHOW_INDEXES, op="show_indexes"),
    }
