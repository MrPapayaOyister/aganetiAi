"""
The Context Engine — one place responsible for gathering context for an LLM call.

    build_context(user_id, message, session_id) -> ContextBundle
                                                        │
                        ┌───────────────────────────────┴─────────────┐
                        │  corporate · memory · graph · calendar      │
                        │  tasks · sql · history   (concurrent)       │
                        └───────────────────────────────┬─────────────┘
                                                        ▼
                                  PromptBuilder.build_sections(bundle)

Before this package, retrieval was inline in `backend/main.py::chat_endpoint`:
four sequential calls returning bare strings, with prompt blocks assembled
between them. Sources could not be added, timed, ranked or reasoned about
without editing the request handler.

Rules this package keeps:
  * providers never see a prompt; the prompt builder never performs retrieval
  * every provider is isolated and timed — one failure cannot affect another
  * no cross-source ranking or merging happens here (that is Phase 4)
  * adding a source is `register(MyProvider())`, nothing else
"""
from .budget import BudgetPolicy, ContextBudget, estimate_tokens
from .bundle import (
    BundleStats, ContextBundle, ContextItem, FusionStats, ProviderStat,
    RankedContextBundle,
)
from .builder import (
    ContextBuilder, build_context, build_ranked_context, get_context_builder,
)
from .compression import ContextCompressor
from .evidence import Evidence
from .fusion import HybridContextFusion, NormalizationPolicy
from .ranking import ProviderReliability, RankingWeights, WeightedRanker
from .prompt import (
    build_sections,
    calendar_text,
    corporate_section,
    graph_section,
    memory_section,
    tasks_text,
)
from .providers import (
    CalendarProvider,
    ContextProvider,
    ContextRequest,
    CorporateKnowledgeProvider,
    GraphProvider,
    HistoryProvider,
    MemoryProvider,
    SQLProvider,
    TaskProvider,
    register,
)
from .providers import registry as provider_registry


def register_default_providers(*, replace: bool = True) -> "list[str]":
    """Register the providers that mirror today's retrieval, once per process.

    Idempotent by default so an import-order surprise cannot raise. The order
    here is cosmetic — the builder runs them concurrently.
    """
    for provider in (
        CorporateKnowledgeProvider(),
        MemoryProvider(),
        GraphProvider(),          # disabled unless CONTEXT_GRAPH_ENABLED=true
        CalendarProvider(),
        TaskProvider(),
        SQLProvider(),            # disabled — no SQL source on the chat path
        HistoryProvider(),
    ):
        register(provider, replace=replace)
    return provider_registry.names()


# Registered at import so `build_context()` works without ceremony at the call site.
register_default_providers()

__all__ = [
    "build_context", "build_ranked_context", "ContextBuilder", "get_context_builder",
    # ── phase 4: hybrid fusion ──
    "RankedContextBundle", "FusionStats", "Evidence",
    "HybridContextFusion", "NormalizationPolicy",
    "WeightedRanker", "RankingWeights", "ProviderReliability",
    "ContextCompressor", "ContextBudget", "BudgetPolicy", "estimate_tokens",
    "ContextBundle", "ContextItem", "ProviderStat", "BundleStats",
    "ContextProvider", "ContextRequest", "register", "register_default_providers",
    "CorporateKnowledgeProvider", "MemoryProvider", "GraphProvider",
    "CalendarProvider", "TaskProvider", "SQLProvider", "HistoryProvider",
    "build_sections", "memory_section", "corporate_section", "graph_section",
    "calendar_text", "tasks_text",
]
