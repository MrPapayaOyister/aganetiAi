"""HTTP surface for the prompt-to-chart Dashboard (additive, auth-enforced).

    POST   /dashboard/chat                     → SSE: token/tool_call/chart_saved/done
    GET    /dashboard/charts?board_id&include_unclaimed  → charts + live data
    GET    /dashboard/charts/{id}              → one chart + data
    POST   /dashboard/charts/{id}/board        → claim an untagged chart to a board
    GET    /dashboard/boards                   → boards with chart counts
    POST   /dashboard/board-session            → mint a new board id
    DELETE /dashboard/boards/{id}              → delete a board's charts

Identity is read ONLY from the trusted X-Auth-User header the auth middleware injects
(same rule as agent_os) — client-supplied ids are never trusted. Chart DATA is queried
live from the read-only Azure DB; chart DEFINITIONS come from the local registry.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.dashboard import coreshare_db, config_db, stream, ask as ask_mod
from backend.orchestrator.router import dashboard_model
from backend.dashboard.tools import register_dashboard_tools
from backend.dashboard.analytics_tools import register_analytics_tools

# Register the 5 chart tools into the shared registry at import time (kept out of the
# primary agent's allow-list). Doing it here means a missing Azure driver only breaks
# the dashboard router, never the core agent.
register_dashboard_tools()
# Register the analytics tools (list_metrics/run_metric) — also kept out of PRIMARY_TOOLS;
# the /dashboard/ask agent supplies them as its own allow-list.
register_analytics_tools()

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _uid(request: Request) -> str:
    u = request.headers.get("x-auth-user")
    if not u:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return u


def _next_month(m: str, k: int) -> str:
    y, mo = int(m[:4]), int(m[5:7]) + k
    y += (mo - 1) // 12
    mo = (mo - 1) % 12 + 1
    return f"{y:04d}-{mo:02d}"


def _add_forecast(data: list) -> list:
    """history rows [{x:'yyyy-MM', y:num}] -> history + 3-month forecast w/ 95% band
    (forecast points carry yhat + range=[lo,hi]); the last actual bridges line & band."""
    try:
        from backend.dashboard import forecast
        hist = sorted((r for r in data if r.get("y") is not None and str(r.get("x"))),
                      key=lambda r: str(r["x"]))
        if len(hist) >= 5:
            hist = hist[:-1]  # drop the current (partial) month so it does not skew the fit
        vals = [float(r["y"] or 0) for r in hist]
        if len(vals) < 4:
            return data
        res = forecast.forecast_series(vals, 3)
        if "error" in res:
            return data
        out = [{"x": str(r["x"]), "y": float(r["y"] or 0)} for r in hist]
        out[-1]["yhat"] = out[-1]["y"]
        out[-1]["range"] = [out[-1]["y"], out[-1]["y"]]
        lm = str(hist[-1]["x"])
        for i, f in enumerate(res["forecast"]):
            out.append({"x": _next_month(lm, i + 1), "yhat": f["point"], "range": [f["low95"], f["high95"]]})
        return out
    except Exception:
        return data


async def _render(cfg: dict) -> dict:
    """Run a chart's SQL live against CORE-SHARE (filters not yet wired → {where} = '')."""
    sql = cfg["sql"].replace("{where}", "")

    def _run():
        import time as _t
        for _a in range(2):
            try:
                return {"data": coreshare_db.run_query_cached(sql), "error": None}
            except Exception as e:  # noqa: BLE001
                if _a == 0:
                    _t.sleep(0.6)  # transient pool/connection contention under a load burst
                    continue
                return {"data": [], "error": str(e)[:200]}

    r = await asyncio.to_thread(_run)
    data = r["data"]
    if cfg["type"] == "forecast" and not r["error"] and data:
        data = _add_forecast(data)
    return {"id": cfg["id"], "type": cfg["type"], "title": cfg["title"],
            "board_id": cfg.get("board_id"), "data": data, "error": r["error"]}


@router.post("/chat")
async def dashboard_chat(request: Request):
    uid = _uid(request)
    body = await request.json()
    message = (body.get("message") or "").strip()
    board_id = body.get("board_id") or ""
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    return StreamingResponse(stream.stream_dashboard(uid, message, board_id, dashboard_model()),
                             media_type="text/event-stream")


@router.post("/ask")
async def dashboard_ask(request: Request):
    """Exact-number analytics Q&A (SSE: token/tool_call/tool_result/done). Distinct from
    /chat: this agent answers in words with real figures from the curated metric whitelist,
    it does NOT build charts."""
    uid = _uid(request)
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    return StreamingResponse(ask_mod.ask_stream(uid, message, None, dashboard_model(), (body.get("board_id") or "").strip() or "analytics"),
                             media_type="text/event-stream")


@router.get("/charts")
async def list_charts(request: Request, board_id: str | None = None, include_unclaimed: bool = False):
    _uid(request)
    cfgs = await asyncio.to_thread(config_db.list_configs, board_id, include_unclaimed)
    _sem = asyncio.Semaphore(4)
    async def _capped(c):
        async with _sem:
            return await _render(c)
    charts = await asyncio.gather(*[_capped(c) for c in cfgs]) if cfgs else []
    return {"charts": list(charts)}


@router.get("/charts/{chart_id}")
async def get_chart(request: Request, chart_id: str):
    _uid(request)
    cfg = await asyncio.to_thread(config_db.get_config, chart_id)
    if not cfg:
        return JSONResponse({"error": "not found"}, status_code=404)
    return await _render(cfg)


@router.delete("/charts/{chart_id}")
async def remove_chart(request: Request, chart_id: str):
    _uid(request)
    ok = await asyncio.to_thread(config_db.delete_chart, chart_id)
    return {"ok": bool(ok), "id": chart_id}


@router.post("/charts/{chart_id}/board")
async def claim_chart(request: Request, chart_id: str):
    _uid(request)
    body = await request.json()
    board_id = (body.get("board_id") or "").strip()
    if not board_id:
        return JSONResponse({"error": "board_id required"}, status_code=400)
    ok = await asyncio.to_thread(config_db.assign_board, chart_id, board_id)
    return {"ok": bool(ok), "id": chart_id, "board_id": board_id}


@router.get("/boards")
async def list_boards(request: Request):
    _uid(request)
    boards = await asyncio.to_thread(config_db.list_boards)
    return {"boards": boards}


@router.post("/board-session")
async def new_board_session(request: Request):
    _uid(request)
    return {"board_id": uuid.uuid4().hex}


@router.delete("/boards/{board_id}")
async def delete_board(request: Request, board_id: str):
    _uid(request)
    n = await asyncio.to_thread(config_db.delete_board, board_id)
    return {"ok": True, "deleted": n, "board_id": board_id}


# --- Pre-warm: keep every saved chart's data hot so boards open instantly ----------
# Walks all saved chart configs on an interval and seeds/refreshes the CORE-SHARE result
# cache for each distinct query. With run_query_cached's stale-while-revalidate, opening
# ANY saved board then serves cached rows immediately (no live wait). Disable: DASH_PREWARM=0.
import os as _os
import threading as _thr

_PREWARM_INTERVAL_S = float(_os.getenv("DASH_PREWARM_INTERVAL_S", "150"))
_PREWARM_FRESH_S = float(_os.getenv("DASH_PREWARM_FRESH_S", "120"))  # refresh entries older than this


def _prewarm_loop() -> None:  # pragma: no cover
    import time as _t
    _t.sleep(8)  # let the app finish booting
    while True:
        try:
            cfgs = config_db.list_configs(None, True) or []
            seen: set = set()
            for c in cfgs:
                sql = (c.get("sql") or "").replace("{where}", "")
                if not sql or sql in seen:
                    continue
                seen.add(sql)
                coreshare_db.warm_query(sql, ttl=_PREWARM_FRESH_S)
                _t.sleep(0.25)  # stagger so pre-warm never bursts the pool
        except Exception:
            pass
        _t.sleep(_PREWARM_INTERVAL_S)


if _os.getenv("DASH_PREWARM", "1") != "0":
    _thr.Thread(target=_prewarm_loop, daemon=True, name="dash-prewarm").start()
