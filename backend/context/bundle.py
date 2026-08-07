"""
ContextBundle — the single structured object every retrieval source writes into.

Before this package, retrieval returned bare strings that the caller
concatenated: `context`, `calendar_context`, `tasks_context`,
`long_term_context`. A string carries no score, no provenance and no timing, so
nothing downstream could rank across sources, cite one, or explain why a turn was
slow. Everything is now a ContextItem.

The bundle is deliberately NOT merged or re-ranked. Each provider sorts its own
items; cross-source ranking is Phase 4. This phase only changes where retrieval
lives, never what it returns.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Rough chars-per-token for the estimate in stats. Not a tokenizer: the number
# exists to spot a context blow-up in logs, not to budget a request.
CHARS_PER_TOKEN = 4


@dataclass(slots=True)
class ContextItem:
    """One retrieved thing, whatever produced it.

    `text` is what would reach a prompt; everything else is what lets a later
    phase decide whether it should.
    """

    text: str
    provider: str                                  # "corporate" | "memory" | …
    source: str = ""                               # file name, entity id, "session"
    score: Optional[float] = None                  # provider-native; NOT comparable across providers
    timestamp: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── phase 4: fusion fields ───────────────────────────────────────────────
    # `score` above is never overwritten — it stays the provider's own number so
    # a ranking bug can always be traced back to what the source actually said.
    raw_score: Optional[float] = None              # copy of `score` at fusion time
    normalized_score: float = 0.0                  # 0–1, COMPARABLE across providers
    final_score: float = 0.0                       # after WeightedRanker
    evidence: list = field(default_factory=list)   # list[Evidence]
    providers: list = field(default_factory=list)  # every provider attesting this
    corroboration: int = 1                         # distinct sources behind it
    signals: dict[str, float] = field(default_factory=dict)   # ranking breakdown

    def __post_init__(self) -> None:
        self.text = (self.text or "").strip()
        if self.raw_score is None:
            self.raw_score = self.score
        if not self.providers:
            self.providers = [self.provider]

    @property
    def is_empty(self) -> bool:
        return not self.text

    @property
    def approx_tokens(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN


@dataclass(slots=True)
class ProviderStat:
    """What one provider did on one turn — the observability this phase adds."""

    provider: str
    items: int = 0
    latency_ms: float = 0.0
    ok: bool = True
    timed_out: bool = False
    error: Optional[str] = None
    skipped: bool = False                          # disabled, or no input to work on
    approx_tokens: int = 0

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        if self.timed_out:
            return "timeout"
        return "ok" if self.ok else "error"


@dataclass(slots=True)
class BundleStats:
    """Turn-level totals. `providers` is keyed by provider name."""

    providers: dict[str, ProviderStat] = field(default_factory=dict)
    total_ms: float = 0.0
    total_items: int = 0
    approx_tokens: int = 0

    @property
    def failed(self) -> list[str]:
        return [n for n, s in self.providers.items() if not s.ok and not s.skipped]

    @property
    def timed_out(self) -> list[str]:
        return [n for n, s in self.providers.items() if s.timed_out]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_ms": round(self.total_ms, 1),
            "total_items": self.total_items,
            "approx_tokens": self.approx_tokens,
            "failed": self.failed,
            "timed_out": self.timed_out,
            # `status` is a derived property, so asdict() drops it — and it is the
            # single most useful field when reading these stats in a log.
            "providers": {n: {**asdict(s), "status": s.status}
                          for n, s in self.providers.items()},
        }


@dataclass(slots=True)
class ContextBundle:
    """Everything gathered for one turn, grouped by provider.

    The named lists mirror the providers that exist today. A NEW provider does
    not need a new field — `extra` holds anything unrecognised — so adding an
    EmailProvider or a SlackProvider never touches this class or the builder.
    """

    corporate: list[ContextItem] = field(default_factory=list)
    memories: list[ContextItem] = field(default_factory=list)
    graph: list[ContextItem] = field(default_factory=list)
    calendar: list[ContextItem] = field(default_factory=list)
    tasks: list[ContextItem] = field(default_factory=list)
    sql: list[ContextItem] = field(default_factory=list)
    history: list[ContextItem] = field(default_factory=list)
    extra: dict[str, list[ContextItem]] = field(default_factory=dict)

    metadata: dict[str, Any] = field(default_factory=dict)
    stats: BundleStats = field(default_factory=BundleStats)

    # Which bundle field each provider name writes into. Anything absent lands
    # in `extra` under its own key.
    _FIELDS = {
        "corporate": "corporate", "memory": "memories", "graph": "graph",
        "calendar": "calendar", "tasks": "tasks", "sql": "sql", "history": "history",
    }

    def put(self, provider: str, items: list[ContextItem]) -> None:
        """File a provider's items into the right slot."""
        attr = self._FIELDS.get(provider)
        if attr:
            setattr(self, attr, list(items))
        else:
            self.extra[provider] = list(items)

    def get(self, provider: str) -> list[ContextItem]:
        attr = self._FIELDS.get(provider)
        return getattr(self, attr) if attr else self.extra.get(provider, [])

    def text_of(self, provider: str, joiner: str = "\n") -> str:
        """The provider's items as one block — how a prompt section is built.

        Returns "" when the provider produced nothing, which is what lets the
        prompt builder omit a section entirely rather than emit an empty header.
        """
        return joiner.join(i.text for i in self.get(provider) if not i.is_empty).strip()

    def first_text(self, provider: str, default: str = "") -> str:
        """The single highest-ranked item's text.

        Some legacy call sites consume exactly one string (corporate RAG used
        `top_k=1`); this preserves that shape without them knowing about items.
        """
        for item in self.get(provider):
            if not item.is_empty:
                return item.text
        return default

    @property
    def all_items(self) -> list[ContextItem]:
        out: list[ContextItem] = []
        for name in self._FIELDS:
            out.extend(self.get(name))
        for items in self.extra.values():
            out.extend(items)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            **{name: [asdict(i) for i in self.get(name)] for name in self._FIELDS},
            "extra": {k: [asdict(i) for i in v] for k, v in self.extra.items()},
            "metadata": self.metadata,
            "stats": self.stats.as_dict(),
        }


@dataclass(slots=True)
class FusionStats:
    """What fusion, ranking, compression and budgeting each did and cost."""

    fusion_ms: float = 0.0
    ranking_ms: float = 0.0
    compression_ms: float = 0.0
    budget_ms: float = 0.0

    items_in: int = 0
    items_after_fusion: int = 0
    items_after_compression: int = 0
    items_after_budget: int = 0

    duplicates_merged: int = 0
    cross_provider_merges: int = 0        # the ones that mattered: two SOURCES agreed
    corroborated_items: int = 0

    tokens_available: int = 0
    tokens_used: int = 0
    dropped_for_budget: int = 0
    allocation: dict[str, int] = field(default_factory=dict)   # provider → tokens granted
    contribution: dict[str, int] = field(default_factory=dict)  # provider → items kept

    @property
    def dedup_ratio(self) -> float:
        """Fraction of incoming items removed by fusion."""
        return 0.0 if not self.items_in else 1 - (self.items_after_fusion / self.items_in)

    @property
    def compression_ratio(self) -> float:
        if not self.items_after_fusion:
            return 0.0
        return 1 - (self.items_after_compression / self.items_after_fusion)

    @property
    def budget_utilization(self) -> float:
        return 0.0 if not self.tokens_available else self.tokens_used / self.tokens_available

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "dedup_ratio": round(self.dedup_ratio, 4),
            "compression_ratio": round(self.compression_ratio, 4),
            "budget_utilization": round(self.budget_utilization, 4),
        }


@dataclass(slots=True)
class RankedContextBundle:
    """The output of the hybrid pipeline: ONE ranked list, plus the grouped view.

    Two views of the same items on purpose. `items` is the ranked, fused,
    budget-trimmed truth used for ordering and citation; `bundle` keeps the
    per-provider grouping so the existing PromptBuilder — which emits a
    [MEMORY] section and a [COMPANY KNOWLEDGE] section — keeps working unchanged.
    """

    items: list[ContextItem] = field(default_factory=list)
    bundle: "ContextBundle" = field(default_factory=lambda: ContextBundle())
    fusion: FusionStats = field(default_factory=FusionStats)

    # ── the PromptBuilder-facing surface, delegated so it is a drop-in ──
    def get(self, provider: str) -> list[ContextItem]:
        return self.bundle.get(provider)

    def text_of(self, provider: str, joiner: str = "\n") -> str:
        return self.bundle.text_of(provider, joiner)

    def first_text(self, provider: str, default: str = "") -> str:
        return self.bundle.first_text(provider, default)

    @property
    def stats(self) -> "BundleStats":
        return self.bundle.stats

    @property
    def metadata(self) -> dict[str, Any]:
        return self.bundle.metadata

    def top(self, k: int) -> list[ContextItem]:
        return self.items[:k]

    def by_provider(self, provider: str) -> list[ContextItem]:
        """Ranked items whose evidence includes `provider` — not just the ones it owned."""
        return [i for i in self.items if provider in (i.providers or [i.provider])]

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [asdict(i) for i in self.items],
            "bundle": self.bundle.as_dict(),
            "fusion": self.fusion.as_dict(),
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
