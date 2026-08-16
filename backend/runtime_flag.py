"""Which agent runtime serves a turn — the controlled migration switch.

The P0 audit established that this codebase has two complete agent runtimes:

  Runtime A  POST /chat            backend/main.py, hand-rolled 4-round tool loop
                                   — currently serving ALL live traffic
  Runtime B  POST /agent/chat      backend/orchestrator, LangGraph StateGraph
                                   — the designated target runtime

Runtime B is the target. Runtime A is not being deleted, and no new capability is
being added to it. This module is how traffic moves between them, one cohort at a
time, with a rollback that is a single environment variable.

SELECTION ORDER (first match wins, most specific first):

    1. RUNTIME_B_DENY_USERS   — explicit opt-OUT. Always beats everything below,
                                so one user can be pinned to Runtime A even while
                                a percentage rollout is running.
    2. RUNTIME_B_SESSIONS     — pin individual sessions. The narrowest possible
                                blast radius: one conversation.
    3. RUNTIME_B_USERS        — pin individual users (supabase uid or email).
    4. RUNTIME_B_PERCENT      — deterministic hash bucket, 0-100.
    5. RUNTIME_B_ENABLED      — global switch, default false.

The percentage bucket is a stable hash of the user id, NOT random: the same user
gets the same answer on every request, so a person does not flip between runtimes
mid-conversation (which would split their history across two stores).

DEFAULTS ARE ZERO-IMPACT. With no environment set, `use_runtime_b()` returns False
for everybody and `/chat` behaves exactly as it does today. Nothing in this module
changes behaviour until an operator opts a cohort in.

See docs/runtime-migration.md for the switch and rollback procedure.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("aganeti.runtime_flag")


def _csv(name: str) -> set[str]:
    return {v.strip().lower() for v in os.getenv(name, "").split(",") if v.strip()}


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _pct(name: str) -> int:
    try:
        return max(0, min(100, int(os.getenv(name, "0"))))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class RuntimeChoice:
    """Which runtime, and WHY. The reason is not decoration — it is emitted on the
    response and into the events spine, so a parity investigation can tell a
    percentage-bucket turn from a pinned-user turn without guessing."""

    runtime: str      # "A" | "B"
    reason: str       # stable machine id of the rule that fired

    @property
    def is_b(self) -> bool:
        return self.runtime == "B"

    def as_dict(self) -> dict:
        return {"runtime": self.runtime, "reason": self.reason}


def _bucket(key: str) -> int:
    """Stable 0-99 bucket for an identity. blake2b rather than hash() because
    Python's hash is salted per process — the same user would land in a different
    bucket after every restart, which is exactly what a canary must not do."""
    if not key:
        return 100  # no identity -> never in any partial rollout
    digest = hashlib.blake2b(key.encode("utf-8", "ignore"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % 100


def choose(user_id: str | None = None, session_id: str | None = None,
           email: str | None = None) -> RuntimeChoice:
    """Decide which runtime serves this turn. Pure — reads only env + arguments."""
    uid = (user_id or "").strip().lower()
    sid = (session_id or "").strip().lower()
    mail = (email or "").strip().lower()
    identities = {i for i in (uid, mail) if i}

    if identities & _csv("RUNTIME_B_DENY_USERS"):
        return RuntimeChoice("A", "deny_user")
    if sid and sid in _csv("RUNTIME_B_SESSIONS"):
        return RuntimeChoice("B", "pinned_session")
    if identities & _csv("RUNTIME_B_USERS"):
        return RuntimeChoice("B", "pinned_user")
    pct = _pct("RUNTIME_B_PERCENT")
    if pct > 0 and uid and _bucket(uid) < pct:
        return RuntimeChoice("B", f"percent:{pct}")
    if _bool("RUNTIME_B_ENABLED", False):
        return RuntimeChoice("B", "global")
    return RuntimeChoice("A", "default")


def use_runtime_b(user_id: str | None = None, session_id: str | None = None,
                  email: str | None = None) -> bool:
    return choose(user_id, session_id, email).is_b


def snapshot() -> dict:
    """The live flag configuration — served by GET /runtime/flag so an operator can
    confirm what is actually in effect before and after a switch, rather than
    inferring it from the deploy they think happened."""
    return {
        "enabled": _bool("RUNTIME_B_ENABLED", False),
        "percent": _pct("RUNTIME_B_PERCENT"),
        "pinned_users": sorted(_csv("RUNTIME_B_USERS")),
        "pinned_sessions": sorted(_csv("RUNTIME_B_SESSIONS")),
        "denied_users": sorted(_csv("RUNTIME_B_DENY_USERS")),
        "default_runtime": "A",
        "target_runtime": "B",
    }
