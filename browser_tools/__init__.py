"""browser_tools — the semantic tool layer over the Playwright worker (Phase C).

    tool call -> validate -> BrowserCommand -> worker -> BrowserObservation
              -> ToolResult

**This package registers nothing.** No registry entry, no `guardrails.TOOL_CATEGORY`
edit, no authorization — those are Phases D and E. It defines the 14 schemas, the
translation both ways, and a durable home for screenshots.

**It does not import from `backend/`.** It therefore does not know `TenantContext`
exists: ownership arrives as four explicit strings that Phase D fills in.

The single path to the worker is `client.WorkerGateway.call()` — one door, so Phase
E has one place to insert authorization and one function to write an anti-bypass
test against.
"""
from .artifacts import (
    ArtifactError, ArtifactRef, ArtifactState, ArtifactStore,
    FilesystemArtifactStore, get_artifact_store, set_artifact_store,
)
from .client import (
    HttpTransport, InProcessTransport, WorkerGateway, get_gateway,
    in_process_gateway, set_gateway,
)
from .outcomes import CORRECTABLE, RETRYABLE, TERMINAL, Outcome, recovery_for
from .results import ElementSummary, ToolResult, from_observation
from .schemas import FORBIDDEN_PARAMS, SCHEMAS, TOOL_NAMES, openai_schemas
from .tools import TOOLS, ToolValidationError, fetch_screenshot

__all__ = [
    "SCHEMAS", "TOOL_NAMES", "openai_schemas", "FORBIDDEN_PARAMS",
    "TOOLS", "ToolValidationError", "fetch_screenshot",
    "ToolResult", "ElementSummary", "from_observation",
    "Outcome", "TERMINAL", "RETRYABLE", "CORRECTABLE", "recovery_for",
    "WorkerGateway", "get_gateway", "set_gateway", "in_process_gateway",
    "InProcessTransport", "HttpTransport",
    "ArtifactStore", "ArtifactRef", "ArtifactState", "ArtifactError",
    "FilesystemArtifactStore", "get_artifact_store", "set_artifact_store",
]
