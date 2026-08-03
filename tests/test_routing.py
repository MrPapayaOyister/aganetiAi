"""Routing tests. The router decides whether a question reaches the database at all, so
a misclassification is invisible to the user except as a confident wrong answer.

Regression under test: a bare "me" in the personal-question guard sent "give me the
total expenditure" to the primary agent, which replied that it had no access to
expenditure records.
"""
import sys

sys.path.insert(0, "/home/matrix/aganetiAi")
from backend.chat import unified  # noqa: E402

FAILS = 0


def check(msg, want):
    global FAILS
    got = unified.route(msg)
    ok = got == want
    if not ok:
        FAILS += 1
    print("  %s  %-6s (want %-6s)  %s" % ("PASS" if ok else "FAIL", got, want, msg[:66]))


print("\n1. THE REGRESSION — polite phrasings must still reach the data agent")
check("Break down approved expenditure by category, and also give me the overall "
      "total and the average grant size.", "data")
check("Give me the total approved expenditure.", "data")
check("Show me the breakdown of aid by emirate.", "data")
check("Tell me how many requests were approved.", "data")
check("Can you get me the average grant size?", "data")

print("\n2. PERSONAL QUESTIONS still belong to the primary agent")
check("How many emails do I have?", "primary")
check("What meetings are on my calendar today?", "primary")
check("Show me my inbox.", "primary")
check("Draft a reply to that message.", "primary")
check("What tasks are due this week?", "primary")

print("\n3. DATA questions unchanged")
check("Break down total approved expenditure by category.", "data")
check("How many aid requests were approved?", "data")
check("What is the average grant size?", "data")
check("Top 5 categories by expenditure", "data")
check("Approved expenditure by emirates", "data")
check("Trend of donations by month", "data")

print("\n4. CHART questions unchanged")
check("Build me a dashboard of expenditure by category.", "chart")
check("Show me a pie chart of aid by emirate.", "chart")
check("Create a bar chart of requests by status.", "chart")

print("\n5. CHATTY messages still fall through to the primary agent")
check("Hello!", "primary")
check("Thanks, that's helpful.", "primary")
check("What can you do?", "primary")

print("\n6. ARABIC")
check("كم عدد الطلبات المعتمدة؟", "data")

print("\n" + "=" * 52)
print("ALL PASS" if not FAILS else str(FAILS) + " FAILURE(S)")
print("=" * 52)
sys.exit(1 if FAILS else 0)
