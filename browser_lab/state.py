"""Browser Lab state — sessions, recorded submissions, sticky failure flags.

All in memory, single process, no database. The lab is a test fixture: its
state should die with the container, and adding persistence would let one test
run contaminate the next.

Two properties this module exists to guarantee (§10.3):

**Resettable.** `reset()` returns everything — sessions, submissions, flags, and
the id counters — to the state a freshly started container is in. Tests that
depend on execution order rot, so every test is entitled to call it first.

**Deterministic.** Ids are drawn from counters that `reset()` zeroes, so the
first session after a reset is always `sess-1` and the first submission is
always `sub-1`. Tests can assert on ids instead of fishing them out of a
response, and a failure reproduces with the same names.

Timestamps are recorded because a submission log without them is hard to read,
but they are wall-clock and therefore the one field tests must not assert on.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from browser_lab import fixtures


@dataclass
class Session:
    """One browser session, keyed by the `lab_session` cookie."""

    id: str
    email: str | None = None
    logged_in: bool = False
    profile: dict[str, str] = field(default_factory=dict)
    profile_saved: bool = False
    # Counts *reaching* validation, so the server_reject failure mode can let
    # the second attempt through. Confirmation-dialog round trips do not count:
    # being asked to confirm is not an attempt at submitting.
    submit_attempts: int = 0
    # time.monotonic() at the last /application render — the clock the
    # delayed_element mode measures against.
    application_rendered_at: float | None = None
    last_submission_id: str | None = None


@dataclass
class Submission:
    """What the server actually received — the point of §10.3's fourth bullet.

    Recorded for rejected attempts too. A lab that only logs successes cannot
    answer "what did the agent try to send", which is exactly the question
    §8.2(b)'s re-validation test needs to ask.
    """

    id: str
    session_id: str
    seq: int
    accepted: bool
    fields: dict[str, Any]
    files: list[dict[str, Any]]
    errors: dict[str, str]
    flags: list[str]
    received_at: str


class LabState:
    """The whole mutable world of the lab. One instance per process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def reset(self) -> None:
        """Return to the known state. Safe to call concurrently and repeatedly."""
        # Guard against re-entry from __init__, where the lock is fresh but the
        # attributes do not exist yet.
        lock = getattr(self, "_lock", threading.Lock())
        with lock:
            self._lock = lock
            self.sessions: dict[str, Session] = {}
            self.submissions: list[Submission] = []
            # Sticky flags survive requests but not a reset. Every failure mode
            # starts off, which is what makes "turn on exactly one" meaningful.
            self.flags: dict[str, bool] = {k: False for k in fixtures.FAILURE_MODES}
            self.delay_ms: int = fixtures.DELAY_MS
            self._session_seq = 0
            self._submission_seq = 0

    # ── sessions ─────────────────────────────────────────────────────────────
    def new_session(self) -> Session:
        with self._lock:
            self._session_seq += 1
            sess = Session(id=f"sess-{self._session_seq}")
            self.sessions[sess.id] = sess
            return sess

    def get_session(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        return self.sessions.get(session_id)

    # ── submissions ──────────────────────────────────────────────────────────
    def record(
        self,
        *,
        session_id: str,
        fields: dict[str, Any],
        files: list[dict[str, Any]],
        accepted: bool,
        errors: dict[str, str] | None = None,
        flags: list[str] | None = None,
    ) -> Submission:
        with self._lock:
            self._submission_seq += 1
            sub = Submission(
                id=f"sub-{self._submission_seq}",
                session_id=session_id,
                seq=self._submission_seq,
                accepted=accepted,
                fields=fields,
                files=files,
                errors=errors or {},
                flags=sorted(flags or []),
                received_at=datetime.now(timezone.utc).isoformat(),
            )
            self.submissions.append(sub)
            return sub

    def submission(self, submission_id: str | None) -> Submission | None:
        if not submission_id:
            return None
        return next((s for s in self.submissions if s.id == submission_id), None)

    # ── flags ────────────────────────────────────────────────────────────────
    def set_flags(self, updates: dict[str, bool]) -> dict[str, bool]:
        """Sticky-set failure modes. Unknown names are the caller's bug — raise."""
        unknown = sorted(set(updates) - set(fixtures.FAILURE_MODES))
        if unknown:
            raise KeyError(f"unknown failure mode(s): {', '.join(unknown)}")
        with self._lock:
            for name, on in updates.items():
                self.flags[name] = bool(on)
            return dict(self.flags)


# Module-level singleton. The app holds a reference; tests import this one.
STATE = LabState()
