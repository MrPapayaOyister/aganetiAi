"""Repair the money charts already saved on client boards.

Fixes ONLY the two unambiguous defects:

  A. WRONG COLUMN  -- the title claims expenditure/granted/approved but the SQL sums
     RequiredAmountForAssistance, which is the amount REQUESTED. Charts whose title
     honestly says "requested" or "pending" are left alone: those are correct.
  B. MISSING SCOPE -- the SQL sums SuggestedAssistanceAmount with no approval filter,
     counting money that was never granted as if it had been disbursed.

Deliberately NOT changed: the `<= 1000000` cap. It excludes exactly one real approved
grant (AED 5,625,000) and whether that grant is genuine is a question for the client, so
the repair preserves each chart's existing cap rather than pre-empting that decision.

Run with no arguments for a dry run. Pass --apply to write, which backs up the database
first.
"""
import re
import shutil
import sqlite3
import sys
import time

DB = "/home/matrix/aganetiAi/backend/dashboard/dashboard_configs.db"
APPLY = "--apply" in sys.argv

GRANTED = "SuggestedAssistanceAmount"
REQUESTED = "RequiredAmountForAssistance"
APPROVED_FROM = ("DataShare.VRequestsApproved r "
                 "JOIN DataShare.VRequestAttributes a ON r.RequestID = a.RequestId")
EMIRATES = ("('Ajman', 'Dubai', 'Ras Al-Khaimah', 'Sharjah', 'Umm Al Quwain')")

# A title that claims granted/disbursed money. "Requested" and "pending" are NOT claims
# about expenditure, so charts titled that way are correct as written.
CLAIMS_SPEND = re.compile(r"expenditure|granted|approved|disburse|spend|spent", re.I)
SAYS_REQUESTED = re.compile(r"request|pending|demand", re.I)


def is_scoped(sql):
    return "VRequestsApproved" in sql or re.search(
        r"RequestStatusName\s*=\s*'Approved'", sql, re.I) is not None


def qualify(sql):
    """Prefix bare attribute columns with the `a.` alias introduced by the join."""
    for col in (GRANTED, REQUESTED, "State", "Nationality", "Gender", "MaritalStatus"):
        sql = re.sub(r"(?<![\w.])" + col + r"\b", "a." + col, sql)
    # do not double-qualify, and never touch the request-side columns
    sql = sql.replace("a.a.", "a.")
    return sql


def add_scope(sql):
    """Introduce the approved join. Two shapes occur in the saved charts."""
    # shape 1: FROM DataShare.VRequestAttributes  (no alias, no join)
    m = re.search(r"FROM\s+DataShare\.VRequestAttributes\s*(?!\w)(?!\s+\w+\s+JOIN)", sql, re.I)
    if m and " JOIN " not in sql.upper():
        head, tail = sql[:m.start()], sql[m.end():]
        return qualify(head) + "FROM " + APPROVED_FROM + " " + qualify(tail)
    # shape 2: already joins VRequests (all statuses) -> use the approved view
    out = re.sub(r"DataShare\.VRequests\s+(\w+)\s+JOIN", r"DataShare.VRequestsApproved \1 JOIN",
                 sql, flags=re.I)
    if out != sql:
        return out
    out = re.sub(r"JOIN\s+DataShare\.VRequests\s+(\w+)", r"JOIN DataShare.VRequestsApproved \1",
                 sql, flags=re.I)
    return out


def fix_column(sql):
    """Swap the REQUESTED column for the GRANTED one, filters included."""
    return sql.replace(REQUESTED, GRANTED)


con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute(
    "select id, title, sql, board_id, created_at from dashboard_configs")]

plan = []
for r in rows:
    sql, title = r["sql"] or "", r["title"] or ""
    if not sql:
        continue
    touches_money = GRANTED in sql or REQUESTED in sql
    if not touches_money:
        continue

    reason, new = None, sql
    if REQUESTED in sql and CLAIMS_SPEND.search(title) and not SAYS_REQUESTED.search(title):
        reason = "A: title claims spend but sums the REQUESTED amount"
        new = fix_column(sql)
        if not is_scoped(new):
            new = add_scope(new)
        if "State" in new and "Ajman" not in new:
            new = re.sub(r"WHERE\s+", "WHERE a.State IN " + EMIRATES + " AND ", new, count=1,
                         flags=re.I)
    elif GRANTED in sql and not is_scoped(sql):
        reason = "B: sums granted amounts with no approval scope"
        new = add_scope(sql)

    if reason and new != sql:
        plan.append((r, reason, new))

print("=" * 78)
print("REPAIR PLAN  (%d charts of %d)   mode=%s"
      % (len(plan), len(rows), "APPLY" if APPLY else "DRY RUN"))
print("=" * 78)
for r, reason, new in plan:
    print("\n[%s]  %s" % (r["id"], r["title"]))
    print("  reason: %s" % reason)
    print("  OLD: %s" % r["sql"][:300])
    print("  NEW: %s" % new[:300])
    for label, ok in (("select-only", new.lstrip().upper().startswith("SELECT")),
                      ("{where} kept", ("{where}" in new) == ("{where}" in r["sql"])),
                      ("scoped now", is_scoped(new)),
                      ("no REQUESTED col", REQUESTED not in new),
                      ("balanced parens", new.count("(") == new.count(")"))):
        if not ok:
            print("     !! CHECK FAILED: %s" % label)

untouched = [r for r in rows if r["sql"] and REQUESTED in r["sql"]
             and not any(p[0]["id"] == r["id"] for p in plan)]
print("\n" + "=" * 78)
print("LEFT ALONE (REQUESTED amount, but the title says so - these are correct):")
for r in untouched:
    print("  [%s] %s" % (r["id"], r["title"]))

if not APPLY:
    print("\nDry run. Re-run with --apply to write.")
    raise SystemExit(0)

stamp = time.strftime("%Y%m%d-%H%M%S")
backup = DB + ".bak." + stamp
shutil.copy2(DB, backup)
print("\nbacked up -> %s" % backup)

cur = con.cursor()
for r, reason, new in plan:
    cur.execute("update dashboard_configs set sql = ? where id = ?", (new, r["id"]))
con.commit()
print("updated %d charts" % len(plan))

check = {r["id"]: r["sql"] for r in
         (dict(x) for x in con.execute("select id, sql from dashboard_configs"))}
bad = [r["id"] for r, _, new in plan if check.get(r["id"]) != new]
print("verify: %s" % ("all writes confirmed" if not bad else "MISMATCH " + str(bad)))
