"""Routing: which agent a question reaches.

The router decides whether a question reaches the database at all, so a
misclassification is invisible to the user except as a confident wrong answer —
"I don't have access to expenditure records" for a question the data agent could
have answered exactly.

Regression under test: a bare "me" in the personal-question guard sent "give me
the total expenditure" to the primary agent.

Converted from a hand-rolled script. It printed PASS/FAIL lines, counted a global
FAILS, and ended in `sys.exit()` AT MODULE SCOPE — which aborted pytest
collection for the ENTIRE suite the moment pytest imported it. Every assertion
below was already here and already passing; none of them had ever run in CI,
because nothing that imports this file could finish.
"""

import pytest

from backend.chat import unified


def route(msg: str) -> str:
    return unified.route(msg)


# ── the regression ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "Break down approved expenditure by category, and also give me the overall "
    "total and the average grant size.",
    "Give me the total approved expenditure.",
    "Show me the breakdown of aid by emirate.",
    "Tell me how many requests were approved.",
    "Can you get me the average grant size?",
])
def test_polite_phrasing_still_reaches_the_data_agent(msg):
    """"give me", "show me", "tell me" are ordinary English, not a signal that
    the question is about the user's own mailbox."""
    assert route(msg) == "data"


# ── genuinely personal questions ─────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "How many emails do I have?",
    "What meetings are on my calendar today?",
    "Show me my inbox.",
    "Draft a reply to that message.",
    "What tasks are due this week?",
])
def test_personal_questions_belong_to_the_primary_agent(msg):
    """The other half of the fix. Loosening the guard must not send someone's
    inbox to the analytics database."""
    assert route(msg) == "primary"


# ── unchanged classes ────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "Break down total approved expenditure by category.",
    "How many aid requests were approved?",
    "What is the average grant size?",
    "Top 5 categories by expenditure",
    "Approved expenditure by emirates",
    "Trend of donations by month",
])
def test_data_questions(msg):
    assert route(msg) == "data"


@pytest.mark.parametrize("msg", [
    "Build me a dashboard of expenditure by category.",
    "Show me a pie chart of aid by emirate.",
    "Create a bar chart of requests by status.",
])
def test_chart_questions(msg):
    """"Build me" / "show me" a chart must outrank the data classification, or
    every chart request returns a table."""
    assert route(msg) == "chart"


@pytest.mark.parametrize("msg", [
    "Hello!",
    "Thanks, that's helpful.",
    "What can you do?",
])
def test_chatty_messages_fall_through_to_the_primary_agent(msg):
    assert route(msg) == "primary"


def test_arabic_data_question():
    """Users here are bilingual; routing that only works in English routes half
    the traffic wrongly."""
    assert route("كم عدد الطلبات المعتمدة؟") == "data"
