"""The single definition of every money metric, shared by BOTH data agents.

Why this module exists
----------------------
The /ask analytics agent (dashboard/ask.py) and the chart-builder (dashboard/stream.py)
each carried their own hand-written prose about what "expenditure" means. They drifted,
and the drift reached client boards:

  * ask.py defines expenditure as SuggestedAssistanceAmount JOINed to VRequestsApproved.
  * stream.py names the same column but never mentions the approval scope -- the JOIN
    appears only inside its forecast bullet. So every non-forecast expenditure chart the
    chart agent saved summed across ALL statuses, counting money that was never granted.
  * Nothing in either prompt forbade using RequiredAmountForAssistance (the amount
    REQUESTED) for a chart titled "Expenditure", and two such charts were saved.

Two agents that define the same word differently will put two different numbers for it on
the same board. The fix is one definition, in code, rendered into both prompts.

No frozen values
----------------
This module states no measured totals. A literal like "(about AED 92.1M)" in a prompt is
worse than useless: the model quotes it as an answer without querying, and it goes stale
the moment the charity approves another request. Metric VALUES come from queries; only
metric DEFINITIONS live here.

The amount filters
------------------
`> 0 AND <= 1000000` was introduced because RequiredAmountForAssistance holds corrupt
values. SuggestedAssistanceAmount does not have that problem, so applying the upper cap to
it silently drops real grants rather than corrupt ones. The two columns therefore get
different hygiene, matching the documented reason for the cap.
"""

# ── Canonical SQL fragments ───────────────────────────────────────────────────
# One row per approved request that has an attributes row. Note the join drops the
# handful of approved requests with no attributes row; that is a known, accepted gap
# and it is stated in the contract text below rather than hidden.
APPROVED_JOIN = ("DataShare.VRequestsApproved r "
                 "JOIN DataShare.VRequestAttributes a ON r.RequestID = a.RequestId")

GRANTED_COLUMN = "a.SuggestedAssistanceAmount"      # money actually awarded
REQUESTED_COLUMN = "a.RequiredAmountForAssistance"  # money asked for -- NOT expenditure

GRANTED_HYGIENE = "a.SuggestedAssistanceAmount > 0"
REQUESTED_HYGIENE = ("a.RequiredAmountForAssistance > 0 "
                     "AND a.RequiredAmountForAssistance <= 1000000")

REAL_EMIRATES = "('Ajman', 'Dubai', 'Ras Al-Khaimah', 'Sharjah', 'Umm Al Quwain')"

TOTAL_EXPENDITURE_SQL = (
    "SELECT SUM(a.SuggestedAssistanceAmount) AS value "
    "FROM " + APPROVED_JOIN + " WHERE " + GRANTED_HYGIENE
)


def metric_contract() -> str:
    """The shared money-metric rules, rendered for a system prompt.

    Kept deliberately short and imperative: every line is a rule a reviewer can check a
    generated query against.
    """
    return (
        "MONEY METRICS - one definition, used by every agent and every chart:\n"
        "- EXPENDITURE / spending / aid granted / disbursed = "
        "SUM(a.SuggestedAssistanceAmount) over " + APPROVED_JOIN + ". "
        "BOTH halves are required: the granted column AND the approved scope. Summing "
        "SuggestedAssistanceAmount over DataShare.VRequestAttributes alone counts money "
        "that was never granted, and is always wrong for anything called expenditure.\n"
        "- RequiredAmountForAssistance is the amount REQUESTED, not granted. Never use it "
        "for expenditure, spending, aid granted or disbursement. If the user genuinely "
        "wants demand rather than spend, use it AND title the result 'Requested' so the "
        "two are never confused.\n"
        "- Amount hygiene: for SuggestedAssistanceAmount filter " + GRANTED_HYGIENE
        + " (zeros only). Do NOT add an upper cap to it -- the cap exists for "
        "RequiredAmountForAssistance, which holds corrupt values; for that column use "
        + REQUESTED_HYGIENE + ".\n"
        "- Approved requests are DataShare.VRequestsApproved, which is exactly "
        "DataShare.VRequests WHERE RequestStatusName = 'Approved'. Either is correct; "
        "omitting the scope entirely is not.\n"
        "- A few approved requests have no VRequestAttributes row, so money metrics are "
        "computed over the joined rows. If a count and a money figure disagree slightly, "
        "that is why -- say so rather than reconciling them by changing the join.\n"
        "- Grouping by emirate: State is the emirate, and it holds junk values (blank and "
        "stray numeric ids). Always add WHERE State IN " + REAL_EMIRATES + ".\n"
        "- Never report NetMonthlyIncome, CurrentSalary or TotalSourcesOfIncome: the "
        "values are corrupt. If asked, say the income data is unreliable in this system.\n"
        "- Each row is an aid REQUEST, not a unique person. One applicant may file "
        "several. Note the distinction whenever it changes the meaning of an answer.\n"
    )


def no_frozen_numbers() -> str:
    """The rule that stops prompt constants being served as answers.

    Live behaviour that motivated it: asked "How many aid requests were approved?", the
    agent answered "A total of 6,200 aid requests were approved" in 879ms having made
    zero tool calls, 3 runs out of 3. It was reading a rounded orientation figure out of
    its own prompt and presenting it as a measured result.
    """
    return (
        "FIGURES COME FROM QUERIES, NEVER FROM THIS PROMPT:\n"
        "- Any number you state must come from a query_data result in THIS turn. Numbers "
        "written in these instructions are orientation only -- they are approximate, they "
        "go stale as the charity approves new requests, and quoting one as an answer is "
        "an error even when it looks close.\n"
        "- If a question asks for a figure, run the query. Never answer a 'how many' or "
        "'how much' question from memory, from context, or from these instructions.\n"
        "- If you cannot query, say you could not retrieve the figure. Do not estimate.\n"
    )
