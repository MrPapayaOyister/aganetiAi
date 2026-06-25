"""Unit tests for native tool dispatch (backend/tools.py)."""
import backend.tools as tools


def test_parse_args_handles_dict_str_and_garbage():
    assert tools._parse_args({"a": 1}) == {"a": 1}
    assert tools._parse_args('{"a": 1}') == {"a": 1}
    assert tools._parse_args("not json") == {}
    assert tools._parse_args(None) == {}


def test_lead_in_create_task_includes_priority_and_due():
    s = tools._lead_in("create_task", {"title": "Call Ahmed", "priority": "high", "due": "2026-06-24"})
    assert "Call Ahmed" in s and "high priority" in s and "2026-06-24" in s


def test_lead_in_create_task_omits_defaults():
    s = tools._lead_in("create_task", {"title": "X", "priority": "medium", "due": "null"})
    assert "priority" not in s and "due" not in s


def test_tool_schemas_have_required_actions():
    names = {t["function"]["name"] for t in tools.TOOL_SCHEMAS}
    assert {"create_task", "complete_task", "draft_email", "schedule_meeting", "get_analytics"} <= names


def test_every_schema_tool_is_allowed_by_guardrails():
    # regression: a tool in the schema but missing from guardrails.ACTION_CATEGORY
    # gets silently denied at dispatch (this exact bug shipped once).
    import backend.guardrails as g
    for t in tools.TOOL_SCHEMAS:
        name = t["function"]["name"]
        assert g.decide(name) != "deny", f"{name} is in TOOL_SCHEMAS but denied by guardrails"


def test_dispatch_routes_to_execute_action(monkeypatch):
    captured = {}
    def fake_exec(action, user_id):
        captured["action"] = action
        captured["user_id"] = user_id
        return "\n\n✅ Task created."
    monkeypatch.setattr(tools, "execute_action", fake_exec)
    out = tools.dispatch_tool_call("create_task", {"title": "Buy milk"}, "user_1")
    assert captured["action"] == {"type": "create_task", "title": "Buy milk"}
    assert captured["user_id"] == "user_1"
    assert "Buy milk" in out and "Task created" in out


def test_dispatch_analytics_does_not_call_execute_action(monkeypatch):
    monkeypatch.setattr(tools, "execute_action", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    monkeypatch.setattr("backend.analytics.run_metric", lambda metric, uid, days: {"human": f"{metric}/{days}"})
    out = tools.dispatch_tool_call("get_analytics", {"metric": "summary", "days": 7}, "user_1")
    assert out == "summary/7"


def test_extract_text_tool_calls_bare_json():
    # model leaked the tool call as raw content (the exact failure that hallucinated)
    content = 'entialAction\n{\n  "name": "create_task",\n  "arguments": {"title": "Call the accountant"}\n}'
    calls = tools.extract_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "create_task"
    assert "accountant" in calls[0]["function"]["arguments"]


def test_extract_text_tool_calls_hermes_wrapper():
    content = '<tool_call>{"name": "get_analytics", "arguments": {"metric": "summary"}}</tool_call>'
    calls = tools.extract_text_tool_calls(content)
    assert calls and calls[0]["function"]["name"] == "get_analytics"


def test_extract_text_tool_calls_ignores_plain_text():
    assert tools.extract_text_tool_calls("Sure, the capital of France is Paris.") == []
    assert tools.extract_text_tool_calls('{"foo": "bar"}') == []  # not a known tool


def test_run_tool_calls_joins_multiple(monkeypatch):
    monkeypatch.setattr(tools, "execute_action", lambda action, uid: "\n\n✅ ok")
    calls = [
        {"function": {"name": "create_task", "arguments": '{"title":"A"}'}},
        {"function": {"name": "complete_task", "arguments": '{"title":"B"}'}},
    ]
    out = tools.run_tool_calls(calls, "user_1")
    assert "A" in out and "B" in out


def test_new_tools_in_schema():
    names = {t["function"]["name"] for t in tools.TOOL_SCHEMAS}
    assert "set_reminder" in names
    assert "web_search" in names


def test_set_reminder_in_action_tools():
    assert "set_reminder" in tools.ACTION_TOOLS


def test_web_search_in_read_tools():
    assert "web_search" in tools.READ_TOOLS


def test_lead_in_set_reminder():
    s = tools._lead_in("set_reminder", {"message": "call bank", "remind_at": "4pm"})
    assert "call bank" in s and "4pm" in s


def test_extract_text_tool_calls_finds_set_reminder():
    content = '<tool_call>{"name": "set_reminder", "arguments": {"message": "review report", "remind_at": "3pm"}}</tool_call>'
    calls = tools.extract_text_tool_calls(content)
    assert calls and calls[0]["function"]["name"] == "set_reminder"


def test_extract_text_tool_calls_finds_web_search():
    content = '{"name": "web_search", "arguments": {"query": "latest AI news"}}'
    calls = tools.extract_text_tool_calls(content)
    assert calls and calls[0]["function"]["name"] == "web_search"
