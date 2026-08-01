"""Backfill the flat-JSON chat history into chat_messages.

The legacy store wrote MEMORY_DIR/chat_{safe(uid)}_{safe(session_key)}.json. Two
things make the filename un-splittable by "_":
  * _safe() maps every non-alphanumeric char to "_", and
  * the legacy config alias "user_1" contains an underscore itself.
So we match the uid by LONGEST KNOWN PREFIX instead of splitting.

Session keys come in three shapes:
  36-char UUID  -> a real assistant thread; reuse the id so existing chat_sessions
                   rows keep matching.
  32-hex        -> a dashboard board_id; mapped through uuid5 and tagged
                   kind='analytics' with board_id preserved.
  anything else -> legacy/test junk ("analytics", "dashboard", "d1"); mapped via
                   uuid5, tagged kind='legacy' and archived so it does not clutter
                   the history list.

Idempotent: rows insert with ON CONFLICT (session_id, seq) DO NOTHING, so a re-run
is a no-op. Run with --dry-run first.

Usage:  .venv/bin/python scripts/migrate_chat_json_to_pg.py [--dry-run]
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Load .env the same way config.settings does, so this runs standalone.
_ROOT = Path(__file__).resolve().parents[1]
for _line in (_ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
sys.path.insert(0, str(_ROOT))

from sqlalchemy import select, func  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from config.settings import MEMORY_DIR  # noqa: E402
from backend.db import models as M  # noqa: E402
from backend.db import sync as dbsync  # noqa: E402
from backend.chat.store import _session_uuid, _is_board_key  # noqa: E402

DRY = "--dry-run" in sys.argv


def known_identities(s) -> list[str]:
    """Every uid string a filename might have been written with, longest first."""
    ids: set[str] = set()
    for u in s.execute(select(M.User)).scalars():
        if u.supabase_uid:
            ids.add(str(u.supabase_uid))
        ids.add(str(u.id))
    try:
        from config.users import USERS
        ids.update(USERS.keys())          # legacy aliases: user_1, user_2
    except Exception:
        pass
    # _safe() rewrites non-alphanumerics, so match against the safe form too.
    import re
    safe = {re.sub(r"[^A-Za-z0-9_.-]", "_", i)[:80] for i in ids}
    return sorted(ids | safe, key=len, reverse=True)


def classify(key: str) -> tuple[str, str | None, bool]:
    """(kind, board_id, archived) for a session key.

    Board keys MUST be tested first: uuid.UUID() happily parses a 32-hex string with
    no dashes, so a dashboard board_id would otherwise be misfiled as a plain
    assistant thread and lose its board link."""
    if _is_board_key(key):
        return "analytics", key.lower(), False
    try:
        uuid.UUID(key)
        return "assistant", None, False
    except (ValueError, TypeError):
        return "legacy", None, True


def main() -> int:
    files = sorted(Path(MEMORY_DIR).glob("chat_*.json"))
    print(f"found {len(files)} chat files in {MEMORY_DIR}")
    if DRY:
        print("DRY RUN — nothing will be written\n")

    stats = {"files": 0, "skipped_unknown_uid": 0, "skipped_empty": 0,
             "sessions": 0, "messages": 0, "already": 0}
    by_kind: dict[str, int] = {}

    with dbsync.session() as s:
        idents = known_identities(s)

        for f in files:
            stem = f.stem[len("chat_"):]                  # strip the "chat_" prefix
            uid = next((i for i in idents if stem.startswith(i + "_")), None)
            if uid is None:
                stats["skipped_unknown_uid"] += 1
                print(f"  skip (unknown uid): {f.name}")
                continue
            key = stem[len(uid) + 1:]

            try:
                rows = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"  skip (unreadable): {f.name}: {e}")
                continue
            if not isinstance(rows, list) or not rows:
                stats["skipped_empty"] += 1
                continue

            user = dbsync.resolve_user(s, uid)
            if user is None:
                stats["skipped_unknown_uid"] += 1
                print(f"  skip (uid not in users): {f.name}")
                continue

            kind, board_id, archived = classify(key)
            by_kind[kind] = by_kind.get(kind, 0) + 1
            sid = _session_uuid(key)
            stats["files"] += 1

            if DRY:
                print(f"  {f.name} -> user={uid} key={key[:12]}… kind={kind} msgs={len(rows)}")
                stats["messages"] += len(rows)
                continue

            sess = s.get(M.ChatSession, sid)
            if sess is None:
                first_user = next((r for r in rows if r.get("role") == "user"), None)
                sess = M.ChatSession(
                    id=sid, org_id=user.org_id, user_id=user.id,
                    title=(first_user or {}).get("content", "")[:60] or None,
                    kind=kind, board_id=board_id or sid.hex, archived=archived,
                )
                s.add(sess)
                s.flush()
                stats["sessions"] += 1

            existing = s.execute(
                select(func.count()).select_from(M.ChatMessage)
                .where(M.ChatMessage.session_id == sid)
            ).scalar_one()
            if existing:
                stats["already"] += 1

            last_ts = None
            for i, r in enumerate(rows, start=1):
                role = r.get("role")
                content = r.get("content") or ""
                if role not in ("user", "assistant", "tool", "system") or not content:
                    continue
                ts = r.get("ts")
                try:
                    created = datetime.fromisoformat(ts) if ts else None
                except Exception:
                    created = None
                if created is None:
                    created = datetime.now(timezone.utc)
                last_ts = created
                stmt = pg_insert(M.ChatMessage.__table__).values(
                    id=uuid.uuid4(), org_id=user.org_id, user_id=user.id, session_id=sid,
                    seq=i, role=role, content=content, status="complete",
                    meta={"backfilled_from": f.name}, created_at=created, updated_at=created,
                ).on_conflict_do_nothing(index_elements=["session_id", "seq"])
                res = s.execute(stmt)
                if res.rowcount:
                    stats["messages"] += 1

            n = s.execute(
                select(func.count()).select_from(M.ChatMessage)
                .where(M.ChatMessage.session_id == sid)
            ).scalar_one()
            sess.message_count = n
            if last_ts and not sess.last_message_at:
                sess.last_message_at = last_ts
            s.commit()

    print("\n--- summary ---")
    for k, v in stats.items():
        print(f"{k:24} {v}")
    print("by kind:", by_kind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
