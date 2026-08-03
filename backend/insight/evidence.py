"""Evidence ledger + deterministic answer verification.

The problem this solves: the analytics agent writes SQL, reads the rows, and then
writes prose about them. Nothing checks that the numbers in the prose are the numbers
in the rows. A live example: asked how many aid requests were approved, the agent
answered "A total of 6,200" in 879ms having run no query at all — it was quoting a
rounded orientation figure out of its own prompt. The true count is 6,411.

Approach — deterministic FIRST, LLM only as a fallback:

  * Every `query_data` result is recorded in a per-run Ledger (a contextvar, so the
    tool layer needs no signature change and nothing leaks between concurrent turns).
  * The Analyst is required to declare the figures it used, each citing an
    `evidence_id` and a `derivation` ("this number is the sum of column y in q1").
  * `verify()` recomputes every declared derivation in PURE PYTHON from the recorded
    rows and compares. It also scans the prose for numbers that were never sourced.

Because the common case passes deterministically, it costs zero extra LLM calls.
An LLM critic is only worth running when this pass has already found something.
"""
from __future__ import annotations

import contextvars
import json
import logging
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

log = logging.getLogger("aganeti.insight.evidence")

# Relative tolerance for a "match". Exact for integers; money is often stated rounded
# or in thousands, so allow a little slack before calling it a mismatch.
REL_TOL = 0.005          # 0.5%
ABS_TOL = 0.51           # lets a .5 rounding pass
MIN_PROSE_NUMBER = 1000  # below this, numbers are usually years/counts/percentages

_NUMERIC_DERIVATIONS = {"cell", "sum", "count", "avg", "mean", "max", "min"}


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if not isinstance(v, float) or math.isfinite(v) else None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, str):
        t = v.strip().replace(",", "").replace("AED", "").replace("%", "").strip()
        try:
            return float(t)
        except ValueError:
            return None
    return None


def close(a: float, b: float) -> bool:
    if a == b:
        return True
    return abs(a - b) <= max(ABS_TOL, REL_TOL * max(abs(a), abs(b)))


@dataclass
class EvidenceRecord:
    """One executed query and the rows it returned."""
    id: str
    sql: str
    rows: list[dict]

    @property
    def columns(self) -> list[str]:
        return list(self.rows[0].keys()) if self.rows else []

    def numeric_columns(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for c in self.columns:
            vals = [_as_float(r.get(c)) for r in self.rows]
            keep = [v for v in vals if v is not None]
            # A column is numeric only if essentially all of it parses.
            if keep and len(keep) >= max(1, int(0.8 * len(vals))):
                out[c] = keep
        return out

    def all_numbers(self) -> set[float]:
        vals: set[float] = set()
        for r in self.rows:
            for v in r.values():
                f = _as_float(v)
                if f is not None:
                    vals.add(f)
        return vals

    def summary(self) -> str:
        cols = ", ".join(self.columns) or "(no columns)"
        return f"{self.id}: {len(self.rows)} row(s); columns: {cols}"


@dataclass
class Ledger:
    """Per-turn record of every query the agent ran."""
    records: list[EvidenceRecord] = field(default_factory=list)

    def record(self, sql: str, rows: list[dict]) -> EvidenceRecord:
        rec = EvidenceRecord(id=f"q{len(self.records) + 1}", sql=sql, rows=rows or [])
        self.records.append(rec)
        return rec

    def get(self, eid: str) -> EvidenceRecord | None:
        eid = (eid or "").strip().lower()
        for r in self.records:
            if r.id == eid:
                return r
        return None

    def all_numbers(self) -> set[float]:
        out: set[float] = set()
        for r in self.records:
            out |= r.all_numbers()
        return out

    def is_empty(self) -> bool:
        return not self.records


# ── contextvar plumbing ───────────────────────────────────────────────────────
# A contextvar keeps the ledger scoped to ONE turn even though many turns run
# concurrently in the same process, without changing Tool.handler's signature.
_LEDGER: contextvars.ContextVar[Ledger | None] = contextvars.ContextVar("insight_ledger", default=None)


def start_ledger() -> Ledger:
    led = Ledger()
    _LEDGER.set(led)
    return led


def current_ledger() -> Ledger | None:
    return _LEDGER.get()


def clear_ledger() -> None:
    _LEDGER.set(None)


# ── the declared-figures contract ─────────────────────────────────────────────
@dataclass
class Figure:
    label: str
    value: float
    evidence_id: str
    derivation: str = "cell"
    column: str | None = None
    unit: str | None = None

    @staticmethod
    def parse(d: dict) -> "Figure | None":
        try:
            v = _as_float(d.get("value"))
            if v is None:
                return None
            return Figure(
                label=str(d.get("label") or "").strip()[:120],
                value=v,
                evidence_id=str(d.get("evidence_id") or "").strip().lower(),
                derivation=str(d.get("derivation") or "cell").strip().lower(),
                column=(str(d["column"]).strip() if d.get("column") else None),
                unit=(str(d["unit"]).strip() if d.get("unit") else None),
            )
        except Exception:
            return None


FIGURES_BLOCK = re.compile(r"```figures\s*(.+?)```", re.S | re.I)


def extract_figures(text: str) -> tuple[str, list[Figure]]:
    """Split the assistant's reply into (prose, declared figures).

    The figures travel in a fenced ```figures block so the model can emit them in
    one pass without a second LLM call. The block is stripped before display.
    """
    if not text:
        return text, []
    m = FIGURES_BLOCK.search(text)
    if not m:
        return text, []
    prose = (text[: m.start()] + text[m.end():]).strip()
    figures: list[Figure] = []
    try:
        raw = json.loads(m.group(1).strip())
        if isinstance(raw, dict):
            raw = raw.get("figures") or []
        for d in raw if isinstance(raw, list) else []:
            f = Figure.parse(d) if isinstance(d, dict) else None
            if f:
                figures.append(f)
    except Exception:
        log.debug("insight: figures block was not valid JSON", exc_info=True)
    return prose, figures


# ── recomputation ─────────────────────────────────────────────────────────────
def _candidate_values(rec: EvidenceRecord, derivation: str, column: str | None) -> list[float]:
    """Every value the stated derivation could legitimately produce.

    The model does not have to name the column: if it does, we check that column;
    if it does not, a match against ANY numeric column counts. That keeps the
    contract cheap to satisfy while still catching invented numbers.
    """
    numeric = rec.numeric_columns()
    if column and column in numeric:
        numeric = {column: numeric[column]}

    out: list[float] = []
    if derivation in ("cell", "reported", ""):
        for vals in numeric.values():
            out.extend(vals)
    elif derivation == "sum":
        out.extend(sum(v) for v in numeric.values() if v)
    elif derivation in ("avg", "mean"):
        out.extend(sum(v) / len(v) for v in numeric.values() if v)
    elif derivation == "max":
        out.extend(max(v) for v in numeric.values() if v)
    elif derivation == "min":
        out.extend(min(v) for v in numeric.values() if v)
    elif derivation == "count":
        out.append(float(len(rec.rows)))
        for vals in numeric.values():          # a COUNT(*) column is also a count
            out.extend(vals)
            if vals:
                out.append(sum(vals))
    else:
        # Unknown/compound derivation (ratio, delta, pct_change …): accept anything
        # derivable from this evidence, so we do not raise false alarms on maths we
        # cannot reconstruct. Stage 3's LLM critic is the backstop for those.
        for vals in numeric.values():
            out.extend(vals)
            if vals:
                out.extend([sum(vals), max(vals), min(vals), sum(vals) / len(vals)])
    return out


# Trailing guard is (?!\d)(?!,\d), NOT (?![\w.]): a number at the end of a sentence is
# followed by "." and an earlier version backtracked on that, matching "95,284" out of
# "95,284,266." and then reporting it as unsourced. (?!,\d) is what forces the whole
# comma-grouped number to be consumed while still allowing a trailing comma in a list.
NUM_IN_PROSE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?!\d)(?!,\d)")

# Four-digit integers in this range are almost always years, not measures.
YEAR_RANGE = (1900, 2100)


@dataclass
class Issue:
    kind: str
    detail: str
    figure: str | None = None
    expected: float | None = None
    stated: float | None = None


@dataclass
class VerifyResult:
    ok: bool
    issues: list[Issue] = field(default_factory=list)
    checked: int = 0
    declared: int = 0
    traced: int = 0
    skipped_reason: str | None = None

    @property
    def status(self) -> str:
        """Four outcomes, ordered by how much we actually proved.

        "failed"     something did not reconcile.
        "verified"   figures were declared and every one was recomputed against its
                     stated derivation — we checked both the number and how it was got.
        "traced"     no figures were declared (the model skipped the contract, which it
                     does roughly half the time), but every substantial number in the
                     prose maps to a value derivable from the recorded rows. Weaker
                     than "verified" because the derivation is inferred, not stated.
        "unverified" there was nothing numeric to check. Never conflate this with
                     "the numbers are right" — that false assurance is the exact
                     failure this mechanism exists to prevent.
        """
        if self.issues:
            return "failed"
        if self.checked > 0:
            return "verified"
        return "traced" if self.traced > 0 else "unverified"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "checked": self.checked,
            "declared": self.declared,
            "traced": self.traced,
            "skipped_reason": self.skipped_reason,
            "issues": [
                {"kind": i.kind, "detail": i.detail, "figure": i.figure,
                 "expected": i.expected, "stated": i.stated}
                for i in self.issues
            ],
        }


def _prose_numbers(prose: str) -> list[tuple[str, float]]:
    """Substantial numbers stated in the prose, deduped, in order.

    Skips small counts and percentages (below MIN_PROSE_NUMBER) and bare four-digit
    years, which are labels rather than measures.
    """
    out: list[tuple[str, float]] = []
    seen: set[float] = set()
    for m in NUM_IN_PROSE.finditer(prose or ""):
        raw = m.group(1)
        val = _as_float(raw)
        if val is None or abs(val) < MIN_PROSE_NUMBER:
            continue
        if "," not in raw and float(val).is_integer() and YEAR_RANGE[0] <= val <= YEAR_RANGE[1]:
            continue
        if val in seen:
            continue
        seen.add(val)
        out.append((raw, val))
    return out


def verify(prose: str, figures: list[Figure], ledger: Ledger) -> VerifyResult:
    """Recompute every declared figure against the recorded rows. Pure Python."""
    if ledger is None or ledger.is_empty():
        # No evidence was gathered this turn. If the answer nevertheless states
        # figures, they came from conversation context (or from nowhere) and NOTHING
        # about them was checked. Not a failure -- a figure verified in an earlier
        # turn may legitimately be repeated -- but it must not read like a greeting.
        stray = _prose_numbers(prose)
        if stray:
            return VerifyResult(
                ok=True, declared=len(figures),
                skipped_reason=("no query ran this turn, yet the answer states "
                                f"{len(stray)} figure(s) ({', '.join(r for r, _ in stray[:3])}"
                                f"{', ...' if len(stray) > 3 else ''}) - none could be checked"))
        return VerifyResult(ok=True, skipped_reason="no queries were run")

    issues: list[Issue] = []
    checked = 0

    # 1) every declared figure must cite real evidence and survive recomputation
    for f in figures:
        rec = ledger.get(f.evidence_id)
        if rec is None:
            issues.append(Issue(
                kind="hallucinated_source",
                detail=f"cites {f.evidence_id or '(none)'}, which was never queried",
                figure=f.label, stated=f.value))
            continue
        checked += 1
        candidates = _candidate_values(rec, f.derivation, f.column)
        if not any(close(f.value, c) for c in candidates):
            nearest = min(candidates, key=lambda c: abs(c - f.value), default=None)
            issues.append(Issue(
                kind="numeric_mismatch",
                detail=(f"stated {f.value:,.2f} as the {f.derivation} of {rec.id}"
                        + (f" column {f.column}" if f.column else "")
                        + (f"; nearest derivable value is {nearest:,.2f}" if nearest is not None
                           else "; no numeric column found")),
                figure=f.label, expected=nearest, stated=f.value))

    # 2) the same label must not carry two different values
    by_label: dict[str, float] = {}
    for f in figures:
        key = re.sub(r"\s+", " ", f.label.strip().lower())
        if not key:
            continue
        if key in by_label and not close(by_label[key], f.value):
            issues.append(Issue(
                kind="internal_inconsistency",
                detail=f"'{f.label}' is stated as both {by_label[key]:,.2f} and {f.value:,.2f}",
                figure=f.label, expected=by_label[key], stated=f.value))
        else:
            by_label.setdefault(key, f.value)

    # 3) every substantial number in the prose must be traceable
    known = ledger.all_numbers() | {f.value for f in figures}
    # sums of each evidence's numeric columns are legitimately quotable totals
    for rec in ledger.records:
        for vals in rec.numeric_columns().values():
            if vals:
                known |= {sum(vals), max(vals), min(vals), sum(vals) / len(vals), float(len(vals))}
    traced = 0
    for raw, val in _prose_numbers(prose):
        if not any(close(val, k) for k in known):
            issues.append(Issue(
                kind="unsourced_figure",
                detail=f"the prose states {raw}, which appears in no query result",
                stated=val))
        else:
            traced += 1

    return VerifyResult(ok=not issues, issues=issues, checked=checked,
                        declared=len(figures), traced=traced)
