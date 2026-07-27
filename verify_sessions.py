"""Verify the chat-session registry: record→list→rename→delete + executor memory intact."""
import asyncio, sys, uuid
sys.path.insert(0, ".")
from backend.orchestrator import conversation as convo
from backend.routes import agent_os
from backend.db.base import SessionLocal
from backend.db import repo

UID = "user_1"
SID = str(uuid.uuid4())
Q = "What was decided in the Q3 budget meeting?"


async def main():
    ok = True
    # 1) simulate a turn's writes + registry record
    convo.append(UID, SID, "user", Q)
    convo.append(UID, SID, "assistant", "The team approved a 12% increase and deferred hiring.")
    await agent_os._record_session(UID, SID, Q)

    async with SessionLocal() as s:
        user = await repo.resolve_user(s, UID)
        rows = await repo.list_chat_sessions(s, user.id)
        mine = next((r for r in rows if str(r.id) == SID), None)
        print(f"1. session listed: {mine is not None} | title={mine.title if mine else None!r} | count={mine.message_count if mine else None}")
        ok &= mine is not None and mine.title == Q[:60] and mine.message_count == 2

    # 2) executor memory (JSON) still intact
    hist = convo.load_full(UID, SID)
    print(f"2. executor history intact: {len(hist)} msgs (expect 2)")
    ok &= len(hist) == 2

    # 3) rename (title_source→user)
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, UID)
        renamed = await repo.rename_chat_session(s, uuid.UUID(SID), user.id, "Budget meeting recap")
        await s.commit()
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, UID)
        rows = await repo.list_chat_sessions(s, user.id)
        mine = next((r for r in rows if str(r.id) == SID), None)
        print(f"3. rename: {renamed} | title now={mine.title if mine else None!r} | source={mine.title_source if mine else None}")
        ok &= renamed and mine and mine.title == "Budget meeting recap" and mine.title_source == "user"

    # 3b) auto-title must NOT clobber a user rename
    await agent_os._record_session(UID, SID, Q)  # another turn
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, UID)
        rows = await repo.list_chat_sessions(s, user.id)
        mine = next((r for r in rows if str(r.id) == SID), None)
        print(f"3b. rename survives re-record: title={mine.title if mine else None!r} (expect 'Budget meeting recap')")
        ok &= mine and mine.title == "Budget meeting recap"

    # 4) delete + unlink
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, UID)
        deleted = await repo.delete_chat_session(s, uuid.UUID(SID), user.id)
        await s.commit()
    convo.delete(UID, SID)
    async with SessionLocal() as s:
        user = await repo.resolve_user(s, UID)
        rows = await repo.list_chat_sessions(s, user.id)
        gone = all(str(r.id) != SID for r in rows)
        print(f"4. delete: {deleted} | gone from list: {gone} | json unlinked: {convo.count(UID, SID) == 0}")
        ok &= deleted and gone and convo.count(UID, SID) == 0

    print("SESSIONS VERIFY:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
