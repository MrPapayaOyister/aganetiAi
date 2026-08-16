"""
Metric primitives — pure functions, no I/O, no framework knowledge.

Kept separate from scoring so every number can be unit-tested against a
hand-worked example. Set-based metrics normalise case and whitespace, because a
golden file written by a human will not match a slug byte-for-byte and an eval
that fails on capitalisation measures nothing useful.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

_WS = re.compile(r"[\s_\-]+")


def norm(value: str) -> str:
    """Comparison key: lowercase, collapse separators. 'MS Graph' == 'ms-graph'."""
    return _WS.sub(" ", str(value or "").strip().lower())


def norm_set(values: Iterable[str]) -> set[str]:
    return {norm(v) for v in values if str(v).strip()}


@dataclass(slots=True)
class PRF:
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def as_dict(self) -> dict:
        return {"precision": round(self.precision, 4), "recall": round(self.recall, 4),
                "f1": round(self.f1, 4), "tp": self.true_positives,
                "fp": self.false_positives, "fn": self.false_negatives}


def prf(predicted: Iterable[str], expected: Iterable[str]) -> PRF:
    """Precision / recall / F1 over sets.

    An empty expectation with an empty prediction is a PASS (1.0), not a
    divide-by-zero — that is the correct outcome for a negative case."""
    p, e = norm_set(predicted), norm_set(expected)
    tp = len(p & e)
    fp = len(p - e)
    fn = len(e - p)
    if not e and not p:
        return PRF(1.0, 1.0, 1.0, 0, 0, 0)
    precision = tp / len(p) if p else 0.0
    recall = tp / len(e) if e else (1.0 if not p else 0.0)
    f1 = 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)
    return PRF(precision, recall, f1, tp, fp, fn)


def precision_at_k(ranked: Sequence[str], expected: Iterable[str], k: int) -> float:
    e = norm_set(expected)
    if not e or k <= 0:
        return 0.0
    top = [norm(x) for x in ranked[:k]]
    return sum(1 for x in top if x in e) / min(k, len(top) or 1)


def recall_at_k(ranked: Sequence[str], expected: Iterable[str], k: int) -> float:
    e = norm_set(expected)
    if not e:
        return 1.0
    top = {norm(x) for x in ranked[:k]}
    return len(top & e) / len(e)


def mrr(ranked: Sequence[str], expected: Iterable[str]) -> float:
    """Reciprocal rank of the FIRST relevant item. 0 when none is retrieved."""
    e = norm_set(expected)
    if not e:
        return 0.0
    for i, item in enumerate(ranked, start=1):
        if norm(item) in e:
            return 1.0 / i
    return 0.0


def resolve_document_identity(
        retrieved: "Sequence[tuple[str, str]]", expected: Iterable[str]) -> list[str]:
    """Pick, per retrieved document, the identifier the dataset addresses it by.

    A retrieved chunk carries two identities — the source filename
    (`emd-0293-….md`) and a storage UUID (`347c5512-…`) — and a case may state
    either. The runner used to emit only the UUID, so a case naming the filename
    scored zero recall even when that exact document was retrieved and ranked
    first. Every case declaring `expected_qdrant_documents` was affected, which
    made the whole Qdrant metric family a measure of identifier convention
    rather than of retrieval.

    Each `retrieved` entry is `(source, document_id)`. If EITHER identity is an
    exact normalised match for something expected, that expected string is
    emitted, so the downstream metrics compare like with like. Otherwise the
    document's own primary identity is emitted and simply will not match.

    Matching stays exact on purpose: a filename is never compared against a UUID
    and no substring or fuzzy rule is applied, so this can only recognise a
    document the retriever genuinely returned — it cannot invent a hit.
    """
    want = norm_set(expected)
    out: list[str] = []
    for source, document_id in retrieved:
        hit = next((alias for alias in (source, document_id)
                    if alias and norm(alias) in want), None)
        out.append(hit or source or document_id or "")
    return out


def dcg(relevances: Sequence[float]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(relevances))


def ndcg(ranked: Sequence[str], relevance: dict, k: int = 10) -> float:
    """Normalised DCG against a {item: graded relevance} map."""
    rel = {norm(key): float(v) for key, v in relevance.items()}
    got = [rel.get(norm(x), 0.0) for x in ranked[:k]]
    ideal = sorted(rel.values(), reverse=True)[:k]
    denom = dcg(ideal)
    return (dcg(got) / denom) if denom else 0.0


def rank_correlation(actual: Sequence[str], expected: Sequence[str]) -> float:
    """Spearman-style agreement over the expected order, mapped to 0–1.

    Only items present in BOTH lists are compared: penalising an ordering for
    items the retriever never saw conflates ranking quality with recall, which
    is measured separately."""
    a = [norm(x) for x in actual]
    common = [norm(x) for x in expected if norm(x) in a]
    if len(common) < 2:
        return 1.0 if common else 0.0
    actual_ranks = [a.index(x) for x in common]
    n = len(common)
    d2 = sum((i - r) ** 2 for i, r in enumerate(actual_ranks))
    rho = 1 - (6 * d2) / (n * (n * n - 1))
    return max(0.0, min(1.0, (rho + 1) / 2))


def keyword_coverage(text: str, keywords: Iterable[str]) -> float:
    """Fraction of expected keywords appearing in the text (substring, normalised)."""
    kws = [norm(k) for k in keywords if str(k).strip()]
    if not kws:
        return 1.0
    hay = norm(text)
    return sum(1 for k in kws if k in hay) / len(kws)


def groundedness(answer: str, context_texts: Iterable[str],
                 min_token_len: int = 4) -> float:
    """Fraction of the answer's content words that appear in the retrieved context.

    A crude but honest hallucination proxy: it cannot prove an answer is correct,
    only that its vocabulary is supported by what was retrieved. Short and
    function words are excluded because they match everything."""
    stop = {"the", "and", "for", "with", "that", "this", "from", "have", "has",
            "was", "were", "are", "you", "your", "their", "there", "which",
            "what", "when", "will", "would", "could", "should", "about", "into"}
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9']+", (answer or "").lower())
             if len(w) >= min_token_len and w not in stop]
    if not words:
        return 1.0
    hay = norm(" ".join(context_texts))
    return sum(1 for w in words if w in hay) / len(words)


def citation_coverage(items: Sequence) -> float:
    """Fraction of context items that carry at least one source identifier.

    An item nothing can be traced back to cannot be cited, so this is the ceiling
    on how much of an answer could ever be attributed."""
    if not items:
        return 0.0
    cited = 0
    for it in items:
        ev = getattr(it, "evidence", None) or []
        has_source = any(
            getattr(e, "source_id", "") or getattr(e, "document_id", None)
            or getattr(e, "conversation_id", None) or getattr(e, "graph_edge", None)
            for e in ev)
        if has_source or getattr(it, "source", ""):
            cited += 1
    return cited / len(items)


def safe_mean(values: Iterable[Optional[float]]) -> float:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0
