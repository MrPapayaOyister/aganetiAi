#!/usr/bin/env python3
"""
Standalone Neo4j integration check.

Exercises the full GraphService surface against the real database and cleans up
after itself. Touches ONLY nodes whose ids are prefixed `_verify_`, so it is safe
to run against a database that holds real data.

    python3 scripts/verify_neo4j.py            # run
    python3 scripts/verify_neo4j.py --keep     # leave the fixtures behind to inspect

Exit code 0 = every assertion passed.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.knowledge_graph import (  # noqa: E402
    GraphService,
    GraphUnavailable,
    NodeLabel,
    RelType,
    bootstrap_schema,
    describe_schema,
    is_enabled,
    verify_connectivity,
)

PREFIX = "_verify_"
USER_ID = f"{PREFIX}user_1"
PROJECT_ID = f"{PREFIX}project_1"
DOC_ID = f"{PREFIX}doc_1"

_passed = _failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  \033[32mPASS\033[0m  {label}" + (f" — {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f" — {detail}" if detail else ""))


def cleanup(svc: GraphService) -> None:
    """Remove only this script's fixtures (DETACH DELETE also drops their edges)."""
    for label, node_id in ((NodeLabel.USER, USER_ID),
                           (NodeLabel.PROJECT, PROJECT_ID),
                           (NodeLabel.DOCUMENT, DOC_ID)):
        try:
            svc.delete_node(label, node_id)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    keep = "--keep" in sys.argv

    print("\n\033[1mNeo4j integration check\033[0m")
    print(f"  uri      : {os.getenv('NEO4J_URI', '(unset)')}")
    print(f"  database : {os.getenv('NEO4J_DATABASE', 'neo4j')}")
    print(f"  enabled  : {is_enabled()}\n")

    print("\033[1m1. Connectivity\033[0m")
    if not verify_connectivity():
        print("  \033[31mFAIL\033[0m  cannot reach Neo4j — is the container up? "
              "(docker ps | grep neo4j)")
        return 1
    check("verify_connectivity()", True)

    svc = GraphService()
    check("ping()", svc.ping())

    print("\n\033[1m2. Schema bootstrap (idempotent)\033[0m")
    first = bootstrap_schema(svc)
    second = bootstrap_schema(svc)          # proves re-running is safe
    check("constraints + indexes applied", not first["failed"],
          f"{len(first['applied'])} objects")
    check("re-running is a no-op", first["applied"] == second["applied"])
    schema = describe_schema(svc)
    names = {c["name"] for c in schema["constraints"]}
    check("User(id) uniqueness constraint exists", "constraint_user_id_unique" in names)

    cleanup(svc)                            # start from a known-clean slate

    print("\n\033[1m3. Create user\033[0m")
    user = svc.merge_node(NodeLabel.USER, USER_ID,
                          {"email": "verify@example.com", "display_name": "Verify User"})
    check("merge_node(User)", user.id == USER_ID, f"id={user.id}")
    check("properties persisted", user.properties.get("email") == "verify@example.com")
    check("created_at stamped", "created_at" in user.properties)

    print("\n\033[1m4. Create project\033[0m")
    project = svc.merge_node(NodeLabel.PROJECT, PROJECT_ID,
                             {"name": "Verify Project", "status": "active"})
    check("merge_node(Project)", project.id == PROJECT_ID, f"id={project.id}")
    doc = svc.merge_node(NodeLabel.DOCUMENT, DOC_ID, {"title": "Spec", "owner_id": USER_ID})
    check("merge_node(Document)", doc.id == DOC_ID)

    print("\n\033[1m5. Merge relationships\033[0m")
    rel = svc.merge_relationship(NodeLabel.USER, USER_ID, RelType.OWNS,
                                 NodeLabel.PROJECT, PROJECT_ID, {"role": "owner"})
    check("merge_relationship(User-OWNS->Project)", rel is not None and rel.type == "OWNS")
    again = svc.merge_relationship(NodeLabel.USER, USER_ID, RelType.OWNS,
                                   NodeLabel.PROJECT, PROJECT_ID, {"role": "owner"})
    check("merge is idempotent (no duplicate edge)", again is not None)
    svc.merge_relationship(NodeLabel.PROJECT, PROJECT_ID, RelType.RELATED_TO,
                           NodeLabel.DOCUMENT, DOC_ID)
    neighbors_after_merge = svc.find_neighbors(NodeLabel.USER, USER_ID)
    check("exactly one edge after two merges", len(neighbors_after_merge) == 1,
          f"got {len(neighbors_after_merge)}")

    print("\n\033[1m6. Retrieve neighbors\033[0m")
    nbrs = svc.find_neighbors(NodeLabel.USER, USER_ID)
    check("user has 1 neighbour", len(nbrs) == 1, f"{[n.node.id for n in nbrs]}")
    check("neighbour is the project", nbrs and nbrs[0].node.id == PROJECT_ID)
    check("direction is outgoing", nbrs and nbrs[0].direction == "out")
    check("relationship property returned", nbrs and nbrs[0].rel_properties.get("role") == "owner")

    proj_nbrs = svc.find_neighbors(NodeLabel.PROJECT, PROJECT_ID)
    check("project sees both sides", len(proj_nbrs) == 2,
          f"{sorted((n.node.id, n.direction) for n in proj_nbrs)}")
    filtered = svc.find_neighbors(NodeLabel.PROJECT, PROJECT_ID, RelType.OWNS)
    check("rel_type filter works", len(filtered) == 1 and filtered[0].direction == "in")

    print("\n\033[1m7. Reads\033[0m")
    found = svc.find_node(NodeLabel.USER, USER_ID)
    check("find_node()", found is not None and found.id == USER_ID)
    check("find_node() misses cleanly", svc.find_node(NodeLabel.USER, f"{PREFIX}nope") is None)
    by_prop = svc.find_nodes_by_property(NodeLabel.USER, "email", "verify@example.com")
    check("find_nodes_by_property()", any(n.id == USER_ID for n in by_prop))
    rows = svc.run_query("MATCH (n:User {id: $id}) RETURN n.email AS email", {"id": USER_ID})
    check("run_query() with parameters", rows and rows[0]["email"] == "verify@example.com")

    print("\n\033[1m8. Delete relationship\033[0m")
    stats = svc.delete_relationship(NodeLabel.USER, USER_ID, RelType.OWNS,
                                    NodeLabel.PROJECT, PROJECT_ID)
    check("delete_relationship()", stats.relationships_deleted == 1,
          f"deleted={stats.relationships_deleted}")
    check("edge is gone", len(svc.find_neighbors(NodeLabel.USER, USER_ID)) == 0)
    check("both nodes survive",
          svc.find_node(NodeLabel.USER, USER_ID) is not None
          and svc.find_node(NodeLabel.PROJECT, PROJECT_ID) is not None)

    print("\n\033[1m9. Safety properties\033[0m")
    try:
        svc.create_node(NodeLabel.USER, USER_ID, {})
        check("duplicate id rejected by constraint", False, "no error raised")
    except Exception as e:  # noqa: BLE001
        check("duplicate id rejected by constraint", "ConstraintValidationFailed" in str(e)
              or "already exists" in str(e).lower(), type(e).__name__)
    try:
        svc.find_node("User) DETACH DELETE (n", "x")
        check("injection-shaped label rejected", False, "no error raised")
    except Exception as e:  # noqa: BLE001
        check("injection-shaped label rejected", type(e).__name__ == "InvalidLabel",
              type(e).__name__)

    if keep:
        print(f"\n  --keep: fixtures left in place ({USER_ID}, {PROJECT_ID}, {DOC_ID})")
    else:
        print("\n\033[1m10. Cleanup\033[0m")
        cleanup(svc)
        check("fixtures removed", svc.find_node(NodeLabel.USER, USER_ID) is None)

    print(f"\n\033[1m{_passed} passed, {_failed} failed\033[0m\n")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GraphUnavailable as e:
        print(f"\n\033[31mNeo4j unavailable:\033[0m {e}")
        sys.exit(1)
