"""One SSE frame vocabulary for every chat surface.

Three endpoints historically spoke three dialects (/agent/chat, /dashboard/ask and
/dashboard/chat each had their own frame set). This module is the single builder so
the merged Assistant has one contract to consume.

ROLLOUT RULE — the new frames are purely ADDITIVE. The deployed frontend's
useStream.handleEvent ignores any `type` it does not recognise, so `start`, `stage`
and `artifact` cost nothing to add. But `token` carries the COMPLETE message and is
applied with REPLACE semantics on the client, so it must keep doing so; true
deltas may only ship behind an explicit client version opt-in.

Frames
------
start     {session_id, message_id?, agent}
stage     {stage, status, label?, ms?, summary?, detail?}     multi-agent progress
tool_call {name, call_id?, stage?, args_preview?}
tool_result {name, call_id?, stage?, ok, rows?, ms?}
token     {content, delta:false}                              COMPLETE message
artifact  {artifact:{id, kind, title, spec, data, meta}}      chart|table|pdf|...
done      {final, message_id?, artifacts[], verified?, stages?}
error     {message, stage?, recoverable?}
"""
from __future__ import annotations

import json
from typing import Any

# Pipeline stages, in the order the UI animates them.
STAGES = ("router", "planner", "query", "analyst", "critic", "final")


def sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, default=str)}\n\n"


def start(session_id: str, agent: str, message_id: str | None = None) -> dict:
    d = {"type": "start", "session_id": session_id, "agent": agent}
    if message_id:
        d["message_id"] = message_id
    return d


def stage(name: str, status: str, *, label: str | None = None, ms: int | None = None,
          summary: str | None = None, detail: Any = None) -> dict:
    d = {"type": "stage", "stage": name, "status": status}
    if label:
        d["label"] = label
    if ms is not None:
        d["ms"] = ms
    if summary:
        d["summary"] = summary
    if detail is not None:
        d["detail"] = detail
    return d


def tool_call(name: str, *, call_id: str | None = None, stage_name: str | None = None,
              args_preview: str | None = None) -> dict:
    d = {"type": "tool_call", "name": name}
    if call_id:
        d["call_id"] = call_id
    if stage_name:
        d["stage"] = stage_name
    if args_preview:
        d["args_preview"] = args_preview[:200]
    return d


def tool_result(name: str, ok: bool, *, call_id: str | None = None,
                stage_name: str | None = None, rows: int | None = None,
                ms: int | None = None) -> dict:
    d = {"type": "tool_result", "name": name, "ok": bool(ok)}
    for k, v in (("call_id", call_id), ("stage", stage_name), ("rows", rows), ("ms", ms)):
        if v is not None:
            d[k] = v
    return d


def token(content: str) -> dict:
    # delta:false is explicit so a future v2 client can tell the two apart.
    return {"type": "token", "content": content, "delta": False}


def artifact(*, id: str, kind: str, title: str | None = None, spec: dict | None = None,
             data: Any = None, meta: dict | None = None) -> dict:
    return {"type": "artifact", "artifact": {
        "id": id, "kind": kind, "title": title,
        "spec": spec or {}, "data": data, "meta": meta or {},
    }}


def done(final: str, *, message_id: str | None = None, artifacts: list[str] | None = None,
         verified: bool | None = None, stages: list[dict] | None = None) -> dict:
    d: dict = {"type": "done", "final": final}
    if message_id:
        d["message_id"] = message_id
    if artifacts:
        d["artifacts"] = artifacts
    if verified is not None:
        d["verified"] = verified
    if stages:
        d["stages"] = stages
    return d


def error(message: str, *, stage_name: str | None = None, recoverable: bool = False) -> dict:
    d = {"type": "error", "message": str(message)[:500], "recoverable": recoverable}
    if stage_name:
        d["stage"] = stage_name
    return d


DONE_SENTINEL = "data: [DONE]\n\n"
