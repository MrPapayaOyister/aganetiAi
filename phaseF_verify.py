"""Phase F in-process verification (run on the DGX inside the venv, from repo root):

    AGANETI_DATA_BACKEND=postgres .venv/bin/python phaseF_verify.py

Covers the three backend-verifiable Phase F pieces:
  1. Multi-tenant org-by-domain  (repo.get_or_create_user derives org from email domain)
  2. Approval audit trail         (store.decide writes an Event kind='approval' w/ decided_by)
  3. Vision end-to-end            (graph.run_turn with an image → vision-vl reads it)

Self-cleaning: every row it creates for the test is deleted before exit.
"""
import asyncio
import base64
import io
import os
import sys
import uuid

sys.path.insert(0, ".")
os.environ.setdefault("AGANETI_DATA_BACKEND", "postgres")

from sqlalchemy import select, delete, desc
from backend.db.base import SessionLocal
from backend.db import repo, models as M
from backend.orchestrator import store, graph
from backend.routes import agent_os

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ── 1. Multi-tenant org-by-domain ─────────────────────────────────────────────
async def test_multitenant():
    print("=== 1. multi-tenant org-by-domain ===")
    async with SessionLocal() as s:
        # existing tenant should have been renamed to the full domain slug by deploy
        org = await repo.get_org_by_slug(s, "meerana.ae")
        check("org 'meerana.ae' exists (migration ran)", org is not None,
              "run: UPDATE organizations SET slug='meerana.ae' WHERE slug='meerana'")
        # a fresh user on a DIFFERENT domain must land in its OWN org (injective slug)
        fake_sub = f"phasef-mt-{uuid.uuid4().hex[:8]}"
        u = await repo.get_or_create_user(s, supabase_uid=fake_sub,
                                          email="probe@acme-phasef.co", full_name="MT Probe")
        await s.flush()
        acme = await repo.get_org_by_slug(s, "acme-phasef.co")
        check("new domain → distinct org", acme is not None and u.org_id == acme.id,
              f"org_id={u.org_id} acme={getattr(acme,'id',None)}")
        # and an existing-domain user reuses the SAME org (no tenant split)
        if org is not None:
            fake_sub2 = f"phasef-mt2-{uuid.uuid4().hex[:8]}"
            u2 = await repo.get_or_create_user(s, supabase_uid=fake_sub2,
                                               email="probe2@meerana.ae", full_name="MT Probe2")
            await s.flush()
            check("existing domain → shared org", u2.org_id == org.id,
                  f"user org_id={u2.org_id} meerana.ae={org.id}")
        await s.rollback()  # discard all probe rows — nothing persisted


# ── 2. Approval audit trail ───────────────────────────────────────────────────
async def test_approval_audit():
    print("=== 2. approval audit (decided_by + Event) ===")
    async with SessionLocal() as s:
        admin = await repo.resolve_user(s, "user_1")
    check("admin resolves", admin is not None)
    if admin is None:
        return
    approval = {"name": "send_email", "action_type": "send_email",
                "preview": "PHASEF audit probe", "tool_call_id": "tc_phasef", "args": {}}
    aid = await store.create_approval("user_1", {"id": "primary"},
                                      [{"role": "user", "content": "x"}], approval)
    check("approval created", bool(aid), f"id={aid}")
    won = await store.decide(aid, "approved", result="ok", decided_by=admin.id)
    check("decide won CAS", won)
    won2 = await store.decide(aid, "approved", result="ok2", decided_by=admin.id)
    check("second decide is a no-op (single-execution)", not won2)
    async with SessionLocal() as s:
        ev = (await s.execute(
            select(M.Event).where(M.Event.kind == "approval",
                                  M.Event.meta["approval_id"].astext == str(aid))
            .order_by(desc(M.Event.ts)).limit(1))).scalar_one_or_none()
        check("audit Event written", ev is not None)
        if ev is not None:
            check("Event.name == 'approved'", ev.name == "approved", ev.name)
            check("meta.decided_by == admin", (ev.meta or {}).get("decided_by") == str(admin.id),
                  str((ev.meta or {}).get("decided_by")))
            check("Event.success True", ev.success is True)
        # cleanup: sweep ALL probe rows (incl. any orphaned by an earlier crashed run) —
        # collect PHASEF approval ids, delete their audit events, then the approvals.
        ids = [str(r[0]) for r in (await s.execute(
            select(M.Approval.id).where(M.Approval.preview.like("PHASEF%")))).all()]
        if ids:
            await s.execute(delete(M.Event).where(
                M.Event.kind == "approval", M.Event.meta["approval_id"].astext.in_(ids)))
            await s.execute(delete(M.Approval).where(M.Approval.preview.like("PHASEF%")))
        await s.commit()


# ── 3. Vision end-to-end ──────────────────────────────────────────────────────
def _text_image_data_url(text: str) -> str:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (360, 120), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 40), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def test_vision():
    print("=== 3. vision end-to-end (graph.run_turn + image) ===")
    token = "PHASEF-" + uuid.uuid4().hex[:4].upper()
    try:
        data_url = _text_image_data_url(token)
    except Exception as e:  # noqa: BLE001
        check("PIL available to build test image", False, str(e))
        return
    agent, prompt = await agent_os._load_primary("user_1")
    res = await graph.run_turn(user_id="user_1", agent=agent,
                               user_message=("Read the exact text shown in the attached image and "
                                             "reply with ONLY that text, nothing else."),
                               system_prompt=prompt, images=[data_url])
    final = (res.get("final") or "")
    check("vision model read the token from the image", token in final,
          f"expected '{token}' in: {final[:120]!r}")


async def main():
    await test_multitenant()
    await test_approval_audit()
    await test_vision()
    print(f"\n=== SUMMARY: {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        sys.exit(1)
    print("ALL PHASE F BACKEND CHECKS PASSED")


asyncio.run(main())
