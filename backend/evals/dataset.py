"""
Golden dataset — the ground truth every metric is measured against.

A case states what SHOULD come back at each stage of the pipeline, so a metric
can be computed per stage rather than only on the final answer. That is the
difference between "the answer got worse" and "graph recall dropped 12% because
alias resolution stopped matching MS Graph".

Every expectation field is OPTIONAL. A case that only asserts
`expected_answer_keywords` is still valid; the runner simply skips the metrics it
has no ground truth for, and `coverage` reports how much of the pipeline that
case actually exercises. Partial cases are worth more than no cases.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

CASES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass(slots=True)
class GoldenCase:
    """One evaluation question plus its ground truth."""

    id: str
    question: str

    # ── stage expectations (all optional) ──
    expected_entities: list[str] = field(default_factory=list)
    expected_graph_nodes: list[str] = field(default_factory=list)
    expected_relationships: list[str] = field(default_factory=list)   # "a|REL|b"
    expected_qdrant_documents: list[str] = field(default_factory=list)
    expected_context_sources: list[str] = field(default_factory=list)  # provider names
    expected_answer_keywords: list[str] = field(default_factory=list)
    expected_provider_order: list[str] = field(default_factory=list)
    expected_corroboration_count: Optional[int] = None

    # ── bookkeeping ──
    user_id: str = "user_1"
    session_id: str = "eval"
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    # A case can be marked as expecting NOTHING — "the graph must not invent an
    # answer for a question about something we have never seen" is a real test.
    negative: bool = False

    @property
    def coverage(self) -> list[str]:
        """Which metric families this case can actually score."""
        out = []
        if self.expected_entities:
            out.append("entity_resolution")
        if self.expected_graph_nodes or self.expected_relationships:
            out.append("graph")
        if self.expected_qdrant_documents:
            out.append("qdrant")
        if self.expected_context_sources or self.expected_provider_order:
            out.append("fusion")
        if self.expected_corroboration_count is not None:
            out.append("corroboration")
        if self.expected_answer_keywords:
            out.append("answer")
        return out

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoldenCase":
        known = {f for f in cls.__slots__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class GoldenDataset:
    """A named collection of cases."""

    name: str = "default"
    cases: list[GoldenCase] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self) -> Iterable[GoldenCase]:
        return iter(self.cases)

    def filter(self, *, tags: Optional[Iterable[str]] = None,
               ids: Optional[Iterable[str]] = None) -> "GoldenDataset":
        want_tags, want_ids = set(tags or []), set(ids or [])
        cases = [c for c in self.cases
                 if (not want_tags or want_tags & set(c.tags))
                 and (not want_ids or c.id in want_ids)]
        return GoldenDataset(name=self.name, cases=cases)

    def coverage_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for case in self.cases:
            for family in case.coverage:
                out[family] = out.get(family, 0) + 1
        return out


def load_dataset(path: "str | Path | None" = None, name: str = "default") -> GoldenDataset:
    """Load every *.json in `cases/` (or one specific file)."""
    target = Path(path) if path else CASES_DIR
    files = [target] if target.is_file() else sorted(target.glob("*.json"))
    cases: list[GoldenCase] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"cannot read golden cases from {f}: {e}") from e
        rows = data.get("cases", data) if isinstance(data, dict) else data
        for row in rows:
            cases.append(GoldenCase.from_dict(row))
    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            raise ValueError(f"duplicate golden case id: {c.id}")
        seen.add(c.id)
    return GoldenDataset(name=name, cases=cases)


def save_dataset(dataset: GoldenDataset, path: "str | Path") -> None:
    Path(path).write_text(
        json.dumps({"cases": [c.as_dict() for c in dataset.cases]}, indent=2),
        encoding="utf-8")
