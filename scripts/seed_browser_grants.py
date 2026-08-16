"""Seed the 14 per-tool browser grants for one canary agent (§4.3, Phase E step 3).

Per-tool grants, not a shared permission string and not a wildcard:

  * a **wildcard** (`browser_*`) is not merely inadvisable, it is unstorable —
    `grant_matches` is exact set membership and the write-time filter at three
    routes drops anything that is not a known tool name or declared permission. It
    would fail as a silent no-grant: an agent that looks configured and is not.
  * a **shared permission string** works, and is what every other tool family here
    uses, but it carries the hazard the wildcard was rejected for — a future
    browser tool declaring the same permission is granted to every existing
    browser-capable agent without anyone deciding that.

Per-tool is free, so the trade-off does not arise.

    python scripts/seed_browser_grants.py --user <supabase-uid>
    python scripts/seed_browser_grants.py --user <supabase-uid> --revoke

Idempotent. Prints what it did, and prints the resulting grant set so the operator
sees the state rather than trusting the exit code.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import backend.orchestrator  # noqa: E402,F401 — registers the tools
from backend.orchestrator.browser_registration import OUTBOUND_TOOLS  # noqa: E402
from browser_tools.schemas import TOOL_NAMES  # noqa: E402


def _primary_agent(s, user):
    from sqlalchemy import select
    from backend.db import models as M
    return s.execute(
        select(M.Agent).where(M.Agent.user_id == user.id, M.Agent.kind == "primary",
                              M.Agent.deleted_at.is_(None))).scalars().first()


def seed(identity: str, *, revoke: bool = False) -> int:
    from sqlalchemy import delete, select
    from backend.db import models as M
    from backend.db import sync as dbsync
    from backend.orchestrator import registry

    names = sorted(TOOL_NAMES)
    # Refuse to write a grant the filter would drop. The failure mode this guards
    # against is silent: an unknown string is dropped, not rejected.
    known = set(registry.all_names()) | registry.all_permissions()
    unknown = [n for n in names if n not in known]
    if unknown:
        print(f"REFUSING: {unknown} are not registered; the write-time filter "
              f"would drop them silently.")
        return 1

    with dbsync.session() as s:
        user = dbsync.resolve_user(s, identity)
        if user is None:
            print(f"no such user: {identity}")
            return 1
        if user.org_id is None:
            print(f"user {identity} has no org; refusing to grant to an unowned agent")
            return 1
        agent = _primary_agent(s, user)
        if agent is None:
            print(f"user {identity} has no primary agent")
            return 1

        if revoke:
            n = s.execute(
                delete(M.AgentPermission)
                .where(M.AgentPermission.agent_id == agent.id,
                       M.AgentPermission.permission.in_(names))).rowcount
            agent_id = str(agent.id)
            s.commit()
            print(f"revoked {n} browser grant(s) from agent {agent_id}")
            return 0

        existing = set(s.execute(
            select(M.AgentPermission.permission)
            .where(M.AgentPermission.agent_id == agent.id)).scalars().all())
        added = 0
        for name in names:
            if name in existing:
                continue
            s.add(M.AgentPermission(
                org_id=user.org_id, agent_id=agent.id, permission=name,
                # Mirrors the registry so an operator reading the table sees which
                # grant is approval-gated. The registry is the authority; this is a
                # copy for legibility, exactly as repo._tool_is_outbound intends.
                is_outbound=(name in OUTBOUND_TOOLS),
                granted_by=user.id))
            added += 1
        s.commit()

        now = sorted(s.execute(
            select(M.AgentPermission.permission)
            .where(M.AgentPermission.agent_id == agent.id,
                   M.AgentPermission.permission.like("browser%"))).scalars().all())
        # Read everything the report needs INSIDE the session. After the block the
        # ORM instances are detached and touching an attribute raises, which would
        # turn a successful seed into a traceback after the commit.
        agent_id, org_id = str(agent.id), str(user.org_id)

    print(f"agent {agent_id} (user {identity}, org {org_id})")
    print(f"  added {added}, already present {len(names) - added}")
    print(f"  browser grants now ({len(now)}): {', '.join(now)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="supabase uid, User.id or email")
    ap.add_argument("--revoke", action="store_true")
    a = ap.parse_args()
    return seed(a.user, revoke=a.revoke)


if __name__ == "__main__":
    sys.exit(main())
