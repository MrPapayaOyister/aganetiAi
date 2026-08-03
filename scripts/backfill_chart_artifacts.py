"""Backfill chart artifacts for analytics threads that predate artifact persistence.

Threads created before the artifact work restore as TEXT ONLY — their charts are
gone from the conversation even though the chart DEFINITIONS still exist in
backend/dashboard/dashboard_configs.db keyed by board_id, and chat_sessions.board_id
links a thread to its board.

This renders each legacy board's charts once and attaches them to the LAST assistant
message of that thread, so old conversations show their charts again.

HONESTY: the figures come from running the saved SQL NOW, not from what the user saw
at the time (no snapshot was taken then). Every backfilled artifact is therefore
tagged meta.backfilled=true and meta.note explaining that, so it is never confused
with a true point-in-time snapshot.

Idempotent: sessions that already have artifacts are skipped.
Usage: .venv/bin/python scripts/backfill_chart_artifacts.py [--dry-run]
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _line in (ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func  # noqa: E402

from backend.db import models as M  # noqa: E402
from backend.db import sync as dbsync  # noqa: E402
from backend.chat.store import _jsonable  # noqa: E402  (Decimal/date -> JSON-safe)

DRY = "--dry-run" in sys.argv

NOTE = ("Restored from the saved chart definition. These figures were produced when the "
        "thread was reopened, not captured at the time of the original answer.")


async def render_board(board_id: str) -> list[dict]:
    from backend.dashboard import config_db
    from backend.routes import dashboard as dash_routes
    try:
        cfgs = await asyncio.to_thread(config_db.list_configs, board_id, False)
    except Exception as e:
        print(f"    ! list_configs failed: {e}")
        return []
    out = []
    for cfg in cfgs or []:
        try:
            out.append(await dash_routes._render(cfg))
        except Exception as e:
            print(f"    ! render failed for {cfg.get('id')}: {e}")
    return out


async def main() -> int:
    made = skipped = empty = 0
    with dbsync.session() as s:
        rows = s.execute(
            select(M.ChatSession).where(
                M.ChatSession.deleted_at.is_(None),
                M.ChatSession.board_id.isnot(None),
            ).order_by(M.ChatSession.last_message_at.desc().nullslast())
        ).scalars().all()
        print(f"{len(rows)} sessions with a board_id")

        for sess in rows:
            have = s.execute(
                select(func.count()).select_from(M.ChatArtifact)
                .where(M.ChatArtifact.session_id == sess.id,
                       M.ChatArtifact.deleted_at.is_(None))
            ).scalar_one()
            if have:
                skipped += 1
                continue

            last_assistant = s.execute(
                select(M.ChatMessage).where(
                    M.ChatMessage.session_id == sess.id,
                    M.ChatMessage.role == "assistant",
                    M.ChatMessage.deleted_at.is_(None),
                ).order_by(M.ChatMessage.seq.desc()).limit(1)
            ).scalar_one_or_none()
            if last_assistant is None:
                continue

            charts = await render_board(sess.board_id)
            good = [c for c in charts if c.get("data") and not c.get("error")]
            if not good:
                empty += 1
                continue

            title = (sess.title or "")[:40]
            print(f"  {'[dry] ' if DRY else ''}{len(good)} chart(s) -> {title!r}")
            if DRY:
                made += len(good)
                continue

            for ch in good:
                s.add(M.ChatArtifact(
                    org_id=sess.org_id, user_id=sess.user_id, session_id=sess.id,
                    message_id=last_assistant.id, kind="chart", title=ch.get("title"),
                    spec=_jsonable({"chart_id": ch.get("id"), "chart_type": ch.get("type"),
                                    "board_id": sess.board_id}),
                    data=_jsonable((ch.get("data") or [])[:200]),
                    meta={"backfilled": True, "note": NOTE},
                ))
                made += 1
            sess.artifact_count = (sess.artifact_count or 0) + len(good)
            s.commit()

    print(f"\nartifacts created: {made}   sessions skipped (already had some): {skipped}   "
          f"boards with no renderable chart: {empty}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
