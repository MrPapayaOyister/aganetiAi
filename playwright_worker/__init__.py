"""playwright-worker — the browser execution backend from §6.

Drives Chromium behind ONE operation:

    execute(BrowserCommand) -> BrowserObservation

Nothing here imports from `backend/`, and nothing in `backend/` imports from here.
The worker holds the ref→locator map, the timeouts and the budgets; it does **not**
authorize (§6.2) — it trusts that its caller already did, and asserts only the
ownership of sessions it created itself.

There is no tool schema in this package. Registry integration is Phase C.
"""
from .config import Budgets, Limits
from .errors import RECOVERY, TERMINAL, ErrorCode, WorkerError
from .protocol import (
    Action, BrowserCommand, BrowserObservation, ElementView, validate_element_ref,
)
from .session import BrowserSession, SessionManager, TaskBudget
from .worker import ARTIFACTS, BrowserWorker, register_artifact

__all__ = [
    "Action", "BrowserCommand", "BrowserObservation", "ElementView",
    "BrowserWorker", "SessionManager", "BrowserSession", "TaskBudget",
    "Budgets", "Limits", "ErrorCode", "WorkerError", "RECOVERY", "TERMINAL",
    "ARTIFACTS", "register_artifact", "validate_element_ref",
]
