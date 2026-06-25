"""
Per-user long-term memory hygiene.

Long-term memory is written automatically every 6h (memory_extract job) into the
Qdrant collection ``user_memory_{user_id}``. Users need a way to inspect and correct
it — otherwise a wrong fact ("you live in Berlin") silently injects into every chat
turn forever. These helpers back the /memory/dump, /memory/forget and /memory/edit
endpoints.
"""

from __future__ import annotations

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from memory.long_term import (
    QDRANT_URL, COLLECTION_PREFIX, embed_text, ensure_collection,
)


def _collection(user_id: str) -> str:
    return f"{COLLECTION_PREFIX}{user_id}"


def _client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def dump_memory(user_id: str, limit: int = 500) -> list[dict]:
    """Return all stored facts for a user: [{id, fact, timestamp}]."""
    client = _client()
    name = _collection(user_id)
    if not client.collection_exists(name):
        return []
    points, _ = client.scroll(collection_name=name, limit=limit, with_payload=True)
    out = []
    for p in points:
        payload = p.payload or {}
        out.append({
            "id": str(p.id),
            "fact": payload.get("fact", ""),
            "timestamp": payload.get("timestamp", ""),
        })
    # newest first when timestamps are present
    out.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return out


def forget_memory(user_id: str, point_id: str | None = None,
                  query: str | None = None) -> dict:
    """
    Delete memory. Either an exact ``point_id``, or the single best semantic match
    for ``query``. Returns {"deleted": [ids], "fact": <deleted fact or None>}.
    """
    client = _client()
    name = _collection(user_id)
    if not client.collection_exists(name):
        return {"deleted": [], "fact": None}

    if point_id:
        # capture the fact for a friendly confirmation, then delete
        existing = client.retrieve(collection_name=name, ids=[point_id], with_payload=True)
        fact = existing[0].payload.get("fact") if existing else None
        client.delete(collection_name=name, points_selector=[point_id])
        return {"deleted": [point_id] if fact is not None else [], "fact": fact}

    if query:
        vec = embed_text(query)
        if vec is None:
            return {"deleted": [], "fact": None}
        res = client.query_points(collection_name=name, query=vec, limit=1, with_payload=True)
        pts = res.points if res else []
        if not pts:
            return {"deleted": [], "fact": None}
        target = pts[0]
        client.delete(collection_name=name, points_selector=[target.id])
        return {"deleted": [str(target.id)], "fact": (target.payload or {}).get("fact")}

    return {"deleted": [], "fact": None}


def edit_memory(user_id: str, point_id: str, new_fact: str) -> dict:
    """Replace the fact text (and its vector) for a given point id."""
    client = _client()
    name = _collection(user_id)
    if not client.collection_exists(name):
        return {"updated": False, "reason": "no memory collection"}

    existing = client.retrieve(collection_name=name, ids=[point_id], with_payload=True)
    if not existing:
        return {"updated": False, "reason": "point not found"}

    vec = embed_text(new_fact)
    if vec is None:
        return {"updated": False, "reason": "embedding failed"}

    old_payload = existing[0].payload or {}
    new_payload = {**old_payload, "fact": new_fact, "edited": True}
    client.upsert(collection_name=name, points=[
        PointStruct(id=point_id, vector=vec, payload=new_payload)
    ])
    return {"updated": True, "fact": new_fact}
