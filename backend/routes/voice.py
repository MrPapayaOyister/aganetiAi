"""Voice WebSocket — WS /ws/voice. Turn-based voice-in / voice-out wired to the
real executor, with per-session conversation context.

Client -> server (JSON):
  {"type":"start"}                                   # optional handshake
  {"type":"audio","format":"wav","data": base64}     # one complete utterance (STT)
  {"type":"text","content":"..."}                    # typed input (fallback/testing)
Server -> client (JSON):
  {"type":"ready"} {"type":"stt","text":...} {"type":"token","content":...}
  {"type":"approval_required","approval":...} {"type":"final","text":...}
  {"type":"audio","format":"wav","data": base64} {"type":"error","message":...}

Auth via ?token= (browsers can't set WS headers): internal token or Supabase bearer.
STT = faster-whisper (integrations.whisper_transcriber), TTS = Kokoro
(integrations.tts). English is fully supported; Arabic STT works via Whisper, AR
TTS needs XTTS-v2 (not yet installed) — flagged as a follow-up.
"""
from __future__ import annotations

import asyncio
import base64
import os
import tempfile

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.orchestrator import graph
from backend.routes.agent_os import DEFAULT_PRIMARY, PRIMARY_PROMPT

router = APIRouter()


def _authed(token: str) -> str | None:
    if not token:
        return None
    try:
        from backend.service_auth import internal_token
        it = internal_token()
        if it and token == it:
            return "user_1"
    except Exception:
        pass
    try:
        from backend.auth.jwt_verify import verify_supabase_jwt
        from config.users import internal_user_for_sub
        claims = verify_supabase_jwt(token)
        return internal_user_for_sub(str(claims.get("sub", "")))
    except Exception:
        return None


async def _stt(wav: bytes) -> str:
    from integrations.whisper_transcriber import transcribe_audio
    fd, path = tempfile.mkstemp(suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(wav)
        return (await asyncio.to_thread(transcribe_audio, path)) or ""
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


async def _tts(text: str) -> bytes | None:
    from integrations.tts import synthesize_speech
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        ok = await asyncio.to_thread(synthesize_speech, text, path)
        if ok and os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        return None
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


@router.websocket("/ws/voice")
async def voice_ws(ws: WebSocket):
    user_id = _authed(ws.query_params.get("token", ""))
    await ws.accept()
    if not user_id:
        await ws.send_json({"type": "error", "message": "unauthorized"})
        await ws.close(code=4401)
        return

    history: list[dict] = []
    session_id = f"voice:{id(ws)}"
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "start":
                await ws.send_json({"type": "ready"})
                continue

            if mtype == "audio":
                try:
                    wav = base64.b64decode(msg.get("data", ""))
                except Exception:
                    await ws.send_json({"type": "error", "message": "bad audio"})
                    continue
                text = (await _stt(wav)).strip()
                await ws.send_json({"type": "stt", "text": text})
            elif mtype == "text":
                text = (msg.get("content") or "").strip()
            else:
                continue

            if not text:
                await ws.send_json({"type": "error", "message": "empty transcript"})
                continue

            parts: list[str] = []
            approval = None
            async for ev in graph.astream_turn(user_id=user_id, agent=DEFAULT_PRIMARY,
                                               user_message=text, session_id=session_id,
                                               system_prompt=PRIMARY_PROMPT, history=history):
                if ev["type"] == "token":
                    parts.append(ev["content"])
                    await ws.send_json({"type": "token", "content": ev["content"]})
                elif ev["type"] == "approval_required":
                    approval = ev["approval"]
                    await ws.send_json({"type": "approval_required", "approval": ev["approval"]})
                elif ev["type"] == "final":
                    msgs = ev.get("messages", [])
                    # keep recent non-system turns as context (avoid duplicating the system prompt)
                    history = [m for m in msgs if m.get("role") != "system"][-12:]

            reply = "".join(parts) or (f"Approval needed: {approval['preview']}" if approval else "")
            await ws.send_json({"type": "final", "text": reply})
            if reply:
                audio = await _tts(reply[:800])
                if audio:
                    await ws.send_json({"type": "audio", "format": "wav",
                                        "data": base64.b64encode(audio).decode()})
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
