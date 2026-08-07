"""Context providers — one module per retrieval source."""
from .base import ContextProvider, ContextRequest
from .calendar import CalendarProvider
from .corporate import CorporateKnowledgeProvider
from .graph import GraphProvider
from .history import HistoryProvider
from .memory import MemoryProvider
from .registry import all_providers, clear, get, names, register, unregister
from .sql import SQLProvider
from .tasks import TaskProvider

__all__ = [
    "ContextProvider", "ContextRequest",
    "CorporateKnowledgeProvider", "MemoryProvider", "GraphProvider",
    "CalendarProvider", "TaskProvider", "SQLProvider", "HistoryProvider",
    "register", "unregister", "get", "all_providers", "names", "clear",
]
