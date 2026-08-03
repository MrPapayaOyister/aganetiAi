"""Unit tests for the deterministic verifier — must catch bad numbers and, just as
importantly, must NOT cry wolf on good ones."""
import sys
from decimal import Decimal

sys.path.insert(0, "/home/matrix/aganetiAi")
from backend.insight import evidence as ev  # noqa: E402

FAILS = 0


def check(name, cond, extra=""):
    global FAILS
    ok = bool(cond)
    if not ok:
        FAILS += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  — ' + extra) if extra and not ok else ''}")


def led_with(rows, sql="SELECT Category AS x, SUM(a) AS y FROM t GROUP BY Category"):
    L = ev.Ledger()
    L.record(sql, rows)
    return L


CATS = [
    {"x": "Treatment Fees", "y": Decimal("45974876")},
    {"x": "Home Rent", "y": Decimal("23838756")},
    {"x": "Study Fees", "y": Decimal("18566850")},
    {"x": "Other", "y": Decimal("6380910")},
    {"x": "Debt", "y": Decimal("522874")},
]
TOTAL = float(sum(float(r["y"]) for r in CATS))  # 95,284,266

print("\n1. HONEST ANSWER — every figure sourced")
L = led_with(CATS)
prose = ("Treatment Fees accounted for AED 45,974,876, ahead of Home Rent at "
         "AED 23,838,756.")
figs = [ev.Figure("Treatment Fees", 45974876, "q1", "cell", "y"),
        ev.Figure("Home Rent", 23838756, "q1", "cell", "y")]
r = ev.verify(prose, figs, L)
check("clean answer verifies", r.ok, str([i.kind for i in r.issues]))
check("both figures were actually checked", r.checked == 2, f"checked={r.checked}")

print("\n2. WRONG NUMBER — figure does not match the rows")
bad = [ev.Figure("Treatment Fees", 52000000, "q1", "cell", "y")]
r = ev.verify("Treatment Fees came to AED 52,000,000.", bad, L)
check("mismatch detected", any(i.kind == "numeric_mismatch" for i in r.issues))
check("nearest real value reported", any(
    i.kind == "numeric_mismatch" and i.expected == 45974876 for i in r.issues))

print("\n3. INVENTED SOURCE — cites a query that never ran")
r = ev.verify("Per q7, spending was AED 1,234,567.",
              [ev.Figure("spend", 1234567, "q7", "cell")], L)
check("hallucinated source detected", any(i.kind == "hallucinated_source" for i in r.issues))

print("\n4. UNSOURCED NUMBER IN PROSE — the 95.6M vs 92.1M class of error")
r = ev.verify("Total approved expenditure was AED 92,139,691 across all categories.",
              [], L)
check("unsourced prose figure detected", any(i.kind == "unsourced_figure" for i in r.issues))

print("\n5. LEGITIMATE TOTAL — the sum of a breakdown must NOT be flagged")
r = ev.verify(f"The categories together account for AED {TOTAL:,.0f}.", [], L)
check("sum of the rows is accepted", not any(i.kind == "unsourced_figure" for i in r.issues),
      str([i.detail for i in r.issues]))

print("\n6. SUM DERIVATION recomputed")
r = ev.verify("Total is AED 95,284,266.",
              [ev.Figure("total", TOTAL, "q1", "sum", "y")], L)
check("correct sum verifies", r.ok, str([i.detail for i in r.issues]))
r = ev.verify("Total is AED 99,000,000.",
              [ev.Figure("total", 99000000, "q1", "sum", "y")], L)
check("wrong sum is caught", any(i.kind == "numeric_mismatch" for i in r.issues))

print("\n7. SAME LABEL, TWO VALUES")
r = ev.verify("...", [ev.Figure("Debt", 522874, "q1", "cell", "y"),
                      ev.Figure("Debt", 522875000, "q1", "cell", "y")], L)
check("internal inconsistency detected",
      any(i.kind == "internal_inconsistency" for i in r.issues))

print("\n8. NO FALSE ALARMS on years, small counts and percentages")
r = ev.verify("In 2026 the top 5 categories covered 68.9% of the 5 rows shown.", [], L)
check("small numbers ignored", r.ok, str([i.detail for i in r.issues]))

print("\n9. ROUNDING TOLERANCE")
r = ev.verify("about AED 45,974,876", [ev.Figure("t", 45974875.6, "q1", "cell", "y")], L)
check("half-unit rounding accepted", r.ok, str([i.detail for i in r.issues]))

print("\n10. COUNT derivation against a row count")
L2 = led_with([{"x": "a"}, {"x": "b"}, {"x": "c"}], "SELECT x FROM t")
r = ev.verify("There are 3 categories.", [ev.Figure("n", 3, "q1", "count")], L2)
check("row-count derivation verifies", r.ok, str([i.detail for i in r.issues]))

print("\n11. NO QUERIES RUN — verification is skipped, not failed")
r = ev.verify("Hello!", [], ev.Ledger())
check("empty ledger skips cleanly", r.ok and r.skipped_reason)

print("\n12. FIGURES BLOCK extraction")
txt = ('Treatment Fees led at AED 45,974,876.\n\n```figures\n'
       '[{"label":"Treatment Fees","value":45974876,"evidence_id":"q1","derivation":"cell"}]\n```')
prose, figs = ev.extract_figures(txt)
check("block stripped from prose", "```figures" not in prose and "45,974,876" in prose)
check("one figure parsed", len(figs) == 1 and figs[0].value == 45974876)
check("malformed block degrades safely", ev.extract_figures("x\n```figures\nnot json\n```")[1] == [])

print("\n13. STATUS — the four outcomes must stay distinct")
L3 = led_with(CATS)
# declared and recomputed -> verified
r = ev.verify("Treatment Fees reached AED 45,974,876.",
              [ev.Figure("Treatment Fees", 45974876, "q1", "cell", "y")], L3)
check("declared + recomputed => verified", r.status == "verified", r.status)

# no block declared, but every prose number traces back to the rows -> traced
r = ev.verify("Treatment Fees reached AED 45,974,876 and Home Rent AED 23,838,756.",
              [], L3)
check("prose-only but sourced => traced", r.status == "traced", r.status)
check("traced count is right", r.traced == 2, "traced=%s" % r.traced)
check("traced still reports ok", r.ok)

# nothing numeric at all -> unverified, and that must NOT read as an assurance
r = ev.verify("The categories are listed above.", [], L3)
check("no numbers => unverified", r.status == "unverified", r.status)

# a bad number outranks everything else
r = ev.verify("Treatment Fees reached AED 45,974,876 but the total was AED 88,888,888.",
              [], L3)
check("one bad number => failed", r.status == "failed", r.status)
check("failed is not ok", not r.ok)

print("\n14. STATUS survives into as_dict")
d = ev.verify("Home Rent was AED 23,838,756.", [], L3).as_dict()
check("as_dict carries status", d.get("status") == "traced", str(d.get("status")))
check("as_dict carries traced", d.get("traced") == 1, str(d.get("traced")))

print(f"\n{'=' * 46}\n{'ALL PASS' if not FAILS else str(FAILS) + ' FAILURE(S)'}\n{'=' * 46}")
sys.exit(1 if FAILS else 0)
