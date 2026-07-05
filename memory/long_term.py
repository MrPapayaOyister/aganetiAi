import httpx
import json
import uuid
import re
import os
from datetime import datetime, timezone
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter,
    FieldCondition, MatchValue
)

QDRANT_URL = "http://localhost:6333"
COLLECTION_PREFIX = "user_memory_"


def _require_uid(user_id) -> str:
    """A blank user_id would silently build `user_memory_`/`user_memory_None` and
    cross-contaminate every user's memory. Refuse it for writes."""
    uid = str(user_id).strip() if user_id is not None else ""
    if not uid:
        raise ValueError("memory: a non-blank user_id is required")
    return uid

# Embeddings are unified on the SAME fastembed model the corporate RAG uses
# (BAAI/bge-small-en-v1.5, 384-dim). Previously memory used the llama.cpp embeddings
# endpoint, whose dimension changed with the loaded model (3584 on a 7B, 5120 on the
# 14B) and silently broke every upsert after a model swap. Sharing one local embedder
# means memory survives model swaps and there is a single dimension to reason about.
_embedder = None

def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        from config.settings import EMBED_MODEL_NAME
        _embedder = TextEmbedding(model_name=EMBED_MODEL_NAME)
    return _embedder

_VECTOR_SIZE = None

def get_vector_size() -> int:
    global _VECTOR_SIZE
    if _VECTOR_SIZE is None:
        probe = embed_text("dimension probe")
        _VECTOR_SIZE = len(probe) if probe else 384
    return _VECTOR_SIZE

def ensure_collection(user_id: str):
    user_id = _require_uid(user_id)
    collection_name = f"{COLLECTION_PREFIX}{user_id}"
    try:
        client = QdrantClient(url=QDRANT_URL)
        if not client.collection_exists(collection_name):
            client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=get_vector_size(), distance=Distance.COSINE)
            )
    except Exception as e:
        print(f"Error ensuring collection {collection_name}: {e}")

def embed_text(text: str) -> list[float] | None:
    try:
        vecs = list(_get_embedder().embed([text]))
        return vecs[0].tolist()
    except Exception as e:
        print(f"Error in embed_text: {e}")
    return None

def extract_facts_from_summary(summary_text: str, user_id: str) -> list[str]:
    url = "http://localhost:8080/v1/chat/completions"
    prompt = f"""Extract atomic facts from this conversation summary. 
Each fact must be a single standalone sentence.
Facts must include names, dates, decisions, preferences, and commitments.
Output ONLY a JSON array of strings. No commentary. No explanation.
Example output: ["Ahmed's deadline is July 1", "User prefers morning meetings"]

Summary:
{summary_text}"""

    payload = {
        "model": "local-model",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0
    }
    try:
        response = httpx.post(url, json=payload, timeout=30.0)
        if response.status_code == 200:
            reply_text = response.json()["choices"][0]["message"]["content"].strip()
            parsed_facts = []
            try:
                start = reply_text.find('[')
                end = reply_text.rfind(']') + 1
                if start != -1 and end != 0:
                    array_text = reply_text[start:end]
                    res = json.loads(array_text)
                    if isinstance(res, list):
                        parsed_facts = [str(item) for item in res]
            except Exception:
                pass
            if not parsed_facts:
                parsed_facts = re.findall(r'"([^"]+)"', reply_text)
            return list(parsed_facts)
    except Exception as e:
        print(f"Error extracting facts: {e}")
    return []

def upsert_facts(facts: list[str], user_id: str, source_timestamp: str):
    try:
        user_id = _require_uid(user_id)
        ensure_collection(user_id)
        points = []
        for fact in facts:
            vector = embed_text(fact)
            if vector is None:
                continue
            points.append(
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vector,
                    payload={"fact": fact, "user_id": user_id, "timestamp": source_timestamp}
                )
            )
        if points:
            print(f"Upserting {len(points)} points to Qdrant...")
            client = QdrantClient(url=QDRANT_URL)
            collection_name = f"{COLLECTION_PREFIX}{user_id}"
            client.upsert(collection_name=collection_name, points=points)
    except Exception as e:
        print(f"Error in upsert_facts: {e}")

def extract_and_store(user_id: str):
    from config.settings import MEMORY_DIR
    summaries_path = str(MEMORY_DIR / user_id / "summaries.json")
    if not os.path.exists(summaries_path):
        return
    try:
        with open(summaries_path, "r", encoding="utf-8") as f:
            summaries = json.load(f)
            if not isinstance(summaries, list):
                return
    except Exception as e:
        print(f"Error loading summaries: {e}")
        return
    updated = False
    for entry in summaries:
        if not entry.get("indexed", False):
            summary_text = entry.get("summary", "")
            timestamp = entry.get("timestamp", "")
            facts = extract_facts_from_summary(summary_text, user_id)
            if facts:
                upsert_facts(facts, user_id, timestamp)
            entry["indexed"] = True
            updated = True
    if updated:
        try:
            with open(summaries_path, "w", encoding="utf-8") as f:
                json.dump(summaries, f, indent=2)
        except Exception as e:
            print(f"Error writing summaries: {e}")

def format_timestamp_for_search(ts_str: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%B %d")
    except Exception:
        return ts_str

def search_memory(user_id: str, query: str, top_k: int = 3) -> str:
    if not user_id or not str(user_id).strip():
        return ""  # never search a blank/None collection
    vector = embed_text(query)
    if vector is None:
        return ""
    client = QdrantClient(url=QDRANT_URL)
    collection_name = f"{COLLECTION_PREFIX}{user_id}"
    try:
        if not client.collection_exists(collection_name):
            return ""
        query_filter = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))])
        search_results = client.query_points(
            collection_name=collection_name,
            query=vector,
            query_filter=query_filter,
            limit=top_k
        )
        all_points = client.scroll(collection_name=collection_name, scroll_filter=query_filter, limit=100)[0]
        stop_words = {"when", "is", "a", "the", "in", "on", "at", "to", "for", "of", "and", "or", "what", "how", "who", "which"}
        query_words = [w.replace("'s", "").strip("?.,!\"'") for w in query.lower().split()]
        query_words = [w for w in query_words if w and w not in stop_words]
        scored_points = []
        seen_ids = set()
        points_to_score = []
        if search_results and search_results.points:
            for p in search_results.points:
                if p.id not in seen_ids:
                    seen_ids.add(p.id)
                    points_to_score.append((p, p.score))
        for p in all_points:
            if p.id not in seen_ids:
                seen_ids.add(p.id)
                points_to_score.append((p, 0.0))
        for result, semantic_score in points_to_score:
            fact_text = result.payload.get("fact", "").lower()
            overlap_score = 0
            for qw in query_words:
                if qw in fact_text:
                    overlap_score += 1.0
            combined_score = semantic_score + (overlap_score * 2.0)
            scored_points.append((combined_score, result))
        scored_points.sort(key=lambda x: x[0], reverse=True)
        top_points = [x[1] for x in scored_points[:top_k]]
        if not top_points:
            return ""
        lines = []
        for result in top_points:
            payload = result.payload
            fact = payload.get("fact", "")
            ts = payload.get("timestamp", "")
            formatted_ts = format_timestamp_for_search(ts)
            if fact:
                lines.append(f"- {fact} (from {formatted_ts})")
        return "\n".join(lines)
    except Exception as e:
        print(f"Error in search_memory: {e}")
    return ""
