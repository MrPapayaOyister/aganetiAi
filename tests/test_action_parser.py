"""Unit tests for the legacy [ACTION:{json}] parser (still a fallback path)."""
from backend.action_parser import extract_action


def test_extracts_trailing_action_tag():
    reply = 'Sure, done.\n[ACTION:{"type":"create_task","title":"Call Ahmed"}]'
    action, clean = extract_action(reply)
    assert action == {"type": "create_task", "title": "Call Ahmed"}
    assert clean == "Sure, done."


def test_no_tag_returns_none_and_original():
    reply = "Just a normal reply with no action."
    action, clean = extract_action(reply)
    assert action is None
    assert clean == reply


def test_malformed_json_is_ignored():
    reply = 'Oops [ACTION:{not valid json}]'
    action, clean = extract_action(reply)
    assert action is None
    assert clean == reply


def test_tag_must_be_at_end():
    # tag not at the tail → not extracted (by design)
    reply = '[ACTION:{"type":"x"}] then more text after'
    action, _ = extract_action(reply)
    assert action is None
