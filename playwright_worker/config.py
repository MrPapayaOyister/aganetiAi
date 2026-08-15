"""Worker configuration. Every §9.2 budget, every §5.4 lifetime, all configurable.

Budgets are read once into a frozen `Budgets` value and then carried on the session,
rather than re-read from the environment per action. Two reasons: a mid-task
environment change must not silently move a budget a task is already being measured
against, and a test needs to construct a worker with tight budgets without mutating
process state that other tests share.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Budgets:
    """§9.2. Enforced worker-side — never by prompt instruction."""

    max_actions_per_task: int = 40
    max_retries_per_element: int = 2
    max_navigations_per_task: int = 10
    action_timeout_ms: int = 15_000
    task_wall_clock_ms: int = 300_000
    max_inspect_elements: int = 60

    @staticmethod
    def from_env() -> "Budgets":
        return Budgets(
            max_actions_per_task=_int("BROWSER_MAX_ACTIONS_PER_TASK", 40),
            max_retries_per_element=_int("BROWSER_MAX_RETRIES_PER_ELEMENT", 2),
            max_navigations_per_task=_int("BROWSER_MAX_NAVIGATIONS_PER_TASK", 10),
            action_timeout_ms=_int("BROWSER_ACTION_TIMEOUT_MS", 15_000),
            task_wall_clock_ms=_int("BROWSER_TASK_WALL_CLOCK_MS", 300_000),
            max_inspect_elements=_int("BROWSER_MAX_INSPECT_ELEMENTS", 60),
        )


@dataclass(frozen=True)
class Limits:
    """§5.4 lifetimes and caps.

    `idle_ttl_s` defaults to 15 min per §5.4, which explicitly notes it must exceed
    the approval window or approvals fail on resume (§8.2a). That interaction is a
    Phase-H concern; the knob exists here so Phase H can raise it without a code
    change, and the worker records the value on every session so a resume failure
    can be attributed rather than guessed at.
    """

    idle_ttl_s: int = 900          # 15 min
    absolute_ttl_s: int = 7_200    # 2 h
    max_sessions_per_user: int = 3
    max_sessions_per_tenant: int = 10
    max_total_sessions: int = 25
    reap_interval_s: int = 30
    screenshot_ttl_s: int = 900
    max_screenshots: int = 50

    @staticmethod
    def from_env() -> "Limits":
        return Limits(
            idle_ttl_s=_int("BROWSER_IDLE_TTL_S", 900),
            absolute_ttl_s=_int("BROWSER_ABSOLUTE_TTL_S", 7_200),
            max_sessions_per_user=_int("BROWSER_MAX_SESSIONS_PER_USER", 3),
            max_sessions_per_tenant=_int("BROWSER_MAX_SESSIONS_PER_TENANT", 10),
            max_total_sessions=_int("BROWSER_MAX_TOTAL_SESSIONS", 25),
            reap_interval_s=_int("BROWSER_REAP_INTERVAL_S", 30),
            screenshot_ttl_s=_int("BROWSER_SCREENSHOT_TTL_S", 900),
            max_screenshots=_int("BROWSER_MAX_SCREENSHOTS", 50),
        )


#: Where the lab lives from inside the worker container (§10.1). The compose
#: service DNS name, never `.local`.
LAB_URL = os.getenv("BROWSER_LAB_URL", "http://browser-lab:8080")
