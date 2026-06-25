"""Unit tests for the natural-language schedule parser."""
from scheduler.schedule_manager import parse_schedule_from_text


def test_every_morning():
    r = parse_schedule_from_text("email digest every morning")
    assert r and r.get("cron_expression") == "0 8 * * *"


def test_every_day_at_time():
    r = parse_schedule_from_text("remind me every day at 9am to check tasks")
    assert r and r.get("cron_expression") == "0 9 * * *"


def test_every_day_at_pm_time():
    r = parse_schedule_from_text("every day at 6:30pm send me a report")
    assert r and r.get("cron_expression") == "30 18 * * *"


def test_gibberish_returns_none():
    assert parse_schedule_from_text("the quick brown fox") is None
