"""The deterministic verifier: catch bad numbers, and do NOT cry wolf on good ones.

Both halves matter equally. A verifier that flags every legitimate total is
turned off within a week, at which point it protects nothing.

Converted from a hand-rolled script. It printed PASS/FAIL lines, counted a
global FAILS, and called `sys.exit()` AT MODULE SCOPE — which aborted pytest
collection for the whole suite as soon as pytest imported it. Every assertion
here was already written and already passing; none had ever run under pytest,
because nothing that imported this file could finish.
"""

from decimal import Decimal

import pytest

from backend.insight import evidence as ev

CATS = [
    {"x": "Treatment Fees", "y": Decimal("45974876")},
    {"x": "Home Rent",      "y": Decimal("23838756")},
    {"x": "Study Fees",     "y": Decimal("18566850")},
    {"x": "Other",          "y": Decimal("6380910")},
    {"x": "Debt",           "y": Decimal("522874")},
]
TOTAL = float(sum(float(r["y"]) for r in CATS))   # 95,284,266


def led_with(rows, sql="SELECT Category AS x, SUM(a) AS y FROM t GROUP BY Category"):
    L = ev.Ledger()
    L.record(sql, rows)
    return L


@pytest.fixture
def ledger():
    return led_with(CATS)


def kinds(result):
    return [i.kind for i in result.issues]


def details(result):
    return [i.detail for i in result.issues]


# ── it accepts honest answers ────────────────────────────────────────────────

def test_a_fully_sourced_answer_verifies(ledger):
    figs = [ev.Figure("Treatment Fees", 45974876, "q1", "cell", "y"),
            ev.Figure("Home Rent", 23838756, "q1", "cell", "y")]
    r = ev.verify("Treatment Fees accounted for AED 45,974,876, ahead of Home "
                  "Rent at AED 23,838,756.", figs, ledger)
    assert r.ok, kinds(r)
    assert r.checked == 2


def test_the_sum_of_a_breakdown_is_not_flagged_as_unsourced(ledger):
    """A total the model derived by adding the rows it was given is legitimate.
    Flagging it is the false positive that gets a verifier switched off."""
    r = ev.verify(f"The categories together account for AED {TOTAL:,.0f}.", [], ledger)
    assert not any(i.kind == "unsourced_figure" for i in r.issues), details(r)


def test_years_small_counts_and_percentages_are_not_figures(ledger):
    r = ev.verify("In 2026 the top 5 categories covered 68.9% of the 5 rows shown.",
                  [], ledger)
    assert r.ok, details(r)


def test_half_unit_rounding_is_accepted(ledger):
    r = ev.verify("about AED 45,974,876",
                  [ev.Figure("t", 45974875.6, "q1", "cell", "y")], ledger)
    assert r.ok, details(r)


# ── it catches dishonest ones ────────────────────────────────────────────────

def test_a_figure_that_does_not_match_the_rows_is_caught(ledger):
    r = ev.verify("Treatment Fees came to AED 52,000,000.",
                  [ev.Figure("Treatment Fees", 52000000, "q1", "cell", "y")], ledger)
    assert any(i.kind == "numeric_mismatch" for i in r.issues)
    assert any(i.kind == "numeric_mismatch" and i.expected == 45974876
               for i in r.issues), "must report the nearest real value"


def test_citing_a_query_that_never_ran_is_caught(ledger):
    r = ev.verify("Per q7, spending was AED 1,234,567.",
                  [ev.Figure("spend", 1234567, "q7", "cell")], ledger)
    assert any(i.kind == "hallucinated_source" for i in r.issues)


def test_an_unsourced_number_in_prose_is_caught(ledger):
    """The 95.6M-vs-92.1M class: a confident total that appears in no query."""
    r = ev.verify("Total approved expenditure was AED 92,139,691 across all "
                  "categories.", [], ledger)
    assert any(i.kind == "unsourced_figure" for i in r.issues)


def test_a_wrong_sum_is_recomputed_and_caught(ledger):
    good = ev.verify("Total is AED 95,284,266.",
                     [ev.Figure("total", TOTAL, "q1", "sum", "y")], ledger)
    assert good.ok, details(good)
    bad = ev.verify("Total is AED 99,000,000.",
                    [ev.Figure("total", 99000000, "q1", "sum", "y")], ledger)
    assert any(i.kind == "numeric_mismatch" for i in bad.issues)


def test_the_same_label_with_two_values_is_caught(ledger):
    r = ev.verify("...", [ev.Figure("Debt", 522874, "q1", "cell", "y"),
                          ev.Figure("Debt", 522875000, "q1", "cell", "y")], ledger)
    assert any(i.kind == "internal_inconsistency" for i in r.issues)


def test_a_count_derivation_checks_the_row_count():
    L2 = led_with([{"x": "a"}, {"x": "b"}, {"x": "c"}], "SELECT x FROM t")
    r = ev.verify("There are 3 categories.", [ev.Figure("n", 3, "q1", "count")], L2)
    assert r.ok, details(r)


# ── nothing to verify is not the same as verified ────────────────────────────

def test_an_empty_ledger_skips_rather_than_fails():
    r = ev.verify("Hello!", [], ev.Ledger())
    assert r.ok and r.skipped_reason


# ── the figures block ────────────────────────────────────────────────────────

def test_the_figures_block_is_stripped_from_the_prose():
    txt = ('Treatment Fees led at AED 45,974,876.\n\n```figures\n'
           '[{"label":"Treatment Fees","value":45974876,'
           '"evidence_id":"q1","derivation":"cell"}]\n```')
    prose, figs = ev.extract_figures(txt)
    assert "```figures" not in prose and "45,974,876" in prose
    assert len(figs) == 1 and figs[0].value == 45974876


def test_a_malformed_figures_block_degrades_instead_of_raising():
    assert ev.extract_figures("x\n```figures\nnot json\n```")[1] == []


# ── the four outcomes must stay distinct ─────────────────────────────────────

def test_declared_and_recomputed_is_verified(ledger):
    r = ev.verify("Treatment Fees reached AED 45,974,876.",
                  [ev.Figure("Treatment Fees", 45974876, "q1", "cell", "y")], ledger)
    assert r.status == "verified"


def test_prose_only_but_sourced_is_traced(ledger):
    r = ev.verify("Treatment Fees reached AED 45,974,876 and Home Rent AED "
                  "23,838,756.", [], ledger)
    assert r.status == "traced"
    assert r.traced == 2
    assert r.ok


def test_no_numbers_at_all_is_unverified_not_an_assurance(ledger):
    """"unverified" must never be presentable as "verified". Nothing was
    checked, and the status has to say so."""
    r = ev.verify("The categories are listed above.", [], ledger)
    assert r.status == "unverified"


def test_one_bad_number_outranks_everything_else(ledger):
    r = ev.verify("Treatment Fees reached AED 45,974,876 but the total was AED "
                  "88,888,888.", [], ledger)
    assert r.status == "failed"
    assert not r.ok


def test_the_status_survives_into_as_dict(ledger):
    """The status is what the UI renders. A status that is correct internally and
    absent from as_dict is a status nobody sees."""
    d = ev.verify("Home Rent was AED 23,838,756.", [], ledger).as_dict()
    assert d.get("status") == "traced"
    assert d.get("traced") == 1
