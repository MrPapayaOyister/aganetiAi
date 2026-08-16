"""The RPC surface (§6.2): one operation, plus health.

    POST /execute            -> BrowserObservation
    GET  /screenshot/{ref}   -> image/png
    GET  /health

`/execute` is deliberately the ONLY way to drive a browser. There is no
`/navigate`, no `/click`, no per-action route — a narrow surface is the point
(§6.2), and a route per action would drift from the closed enum the moment someone
added one without updating the other.

**Authorization.** The worker does not authorize (§6.2); it trusts its caller and
must therefore be reachable only from the orchestrator. §6.2's `[DECIDE]` recommends
a session-scoped token as defence-in-depth against in-cluster SSRF. Implemented as a
single shared bearer (`WORKER_TOKEN`) rather than per-session: a per-session token
requires the orchestrator to hold worker state, which is the coupling §6.1 exists to
avoid. Recorded in the §6 contradictions as a partial implementation of that
recommendation.

`/screenshot/{ref}` is what makes "stored by reference, never inline" usable: the
observation carries the ref, and something with the right identity fetches the bytes
separately.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .config import LAB_URL
from .errors import ErrorCode, WorkerError
from .worker import BrowserWorker

log = logging.getLogger("playwright_worker.service")

#: Shared secret. Absent = open, which is only acceptable behind a private network
#: and is warned about loudly at startup.
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")

app = FastAPI(title="playwright-worker", docs_url=None, redoc_url=None)
WORKER = BrowserWorker()


@app.on_event("startup")
async def _startup() -> None:
    if not WORKER_TOKEN:
        log.warning("WORKER_TOKEN is unset — /execute is unauthenticated. "
                    "Acceptable only on a private network (§6.2).")
    await WORKER.start()
    log.info("playwright-worker ready (lab=%s)", LAB_URL)


@app.on_event("shutdown")
async def _shutdown() -> None:
    # Without this, every live BrowserContext leaks on redeploy — §5.4's "a leaked
    # context is a leaked authenticated browser".
    await WORKER.stop()


def _authenticate(authorization: str | None) -> None:
    if not WORKER_TOKEN:
        return
    expected = f"Bearer {WORKER_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="worker token required")


@app.post("/execute")
async def execute(request: Request, authorization: str | None = Header(default=None)):
    """The one operation. Always 200 with a typed observation — a transport-level
    error code would give the caller two error channels to reconcile."""
    _authenticate(authorization)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    if not isinstance(payload, dict):
        obs = WORKER and None
        from .protocol import BrowserObservation
        return BrowserObservation.failure(
            "unknown", WorkerError(ErrorCode.BAD_REQUEST, "body must be a JSON object")
        ).as_dict()
    observation = await WORKER.execute(payload)
    return observation.as_dict()


@app.get("/screenshot/{ref}")
async def screenshot(ref: str, request: Request,
                     authorization: str | None = Header(default=None)):
    """Fetch stored bytes by reference. Ownership is asserted against the headers the
    orchestrator forwards — the same identity that took the shot."""
    _authenticate(authorization)
    tenant_id = request.headers.get("x-tenant-id", "")
    user_id = request.headers.get("x-user-id", "")
    try:
        shot = WORKER.screenshots.get(ref, tenant_id=tenant_id, user_id=user_id)
    except WorkerError:
        # Same answer for expired, unknown and someone-else's (§5.1).
        raise HTTPException(status_code=404, detail="no such screenshot") from None
    return Response(content=shot.png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/session/{browser_session_id}")
async def session_facts(browser_session_id: str, request: Request,
                        authorization: str | None = Header(default=None)):
    """Owner + live page host, for the caller's authorization decision.

    404 for a session that does not exist and for one owned by another identity —
    the same answer, so this cannot be used to enumerate other tenants' sessions.
    """
    _authenticate(authorization)
    facts = WORKER.session_facts(
        browser_session_id,
        tenant_id=request.headers.get("x-tenant-id", ""),
        user_id=request.headers.get("x-user-id", ""))
    if facts is None:
        raise HTTPException(status_code=404, detail="no such browser session")
    return facts


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", **WORKER.sessions.stats()}
