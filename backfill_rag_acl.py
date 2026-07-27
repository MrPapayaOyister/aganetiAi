"""Backfill: tag every existing RAG chunk that lacks a user_id as the shared org
corpus ('__org__'), so they remain visible to all users after ACL filtering goes
live. Idempotent. Run inside the venv from repo root."""
import sys
sys.path.insert(0, ".")

from backend.ingest import get_client, ORG_OWNER
from config.settings import RAG_COLLECTION


def _distribution(client):
    dist, offset = {}, None
    while True:
        pts, offset = client.scroll(collection_name=RAG_COLLECTION, limit=256,
                                    offset=offset, with_payload=True, with_vectors=False)
        if not pts:
            break
        for p in pts:
            u = (p.payload or {}).get("user_id", "<none>")
            dist[u] = dist.get(u, 0) + 1
        if offset is None:
            break
    return dist


def main():
    client = get_client()
    if not client.collection_exists(RAG_COLLECTION):
        print("no RAG collection — nothing to backfill")
        return
    print("before:", _distribution(client))
    offset, total, tagged = None, 0, 0
    while True:
        pts, offset = client.scroll(collection_name=RAG_COLLECTION, limit=256,
                                    offset=offset, with_payload=True, with_vectors=False)
        if not pts:
            break
        total += len(pts)
        ids = [p.id for p in pts if not (p.payload or {}).get("user_id")]
        if ids:
            client.set_payload(collection_name=RAG_COLLECTION,
                               payload={"user_id": ORG_OWNER}, points=ids)
            tagged += len(ids)
        if offset is None:
            break
    print(f"backfill: scanned {total} points, tagged {tagged} as '{ORG_OWNER}'")
    print("after: ", _distribution(client))


if __name__ == "__main__":
    main()
