"""Tool reachability, in three parts.

This file used to assert one thing: every tool in TOOL_SCHEMAS reaches the
model. Tool toggles deliberately break that — a disabled group is absent from
the payload on purpose. So the property is split into the three that are each
still true, rather than deleted:

  1. REGISTRATION — every tool is dispatchable, guardrailed and grouped. This is
     the invariant that catches the _might_need_tools class of bug, where a tool
     existed but the model was never told about it.
  2. DEFAULT REACHABILITY — with no preferences stored, every tool reaches the
     model. Pins "absent means enabled".
  3. FILTER HONESTY — a disabled group is absent from the built payload, not
     merely refused at dispatch.
"""

import backend.guardrails as guardrails
import backend.tools as tools


# ── 1. registration ──────────────────────────────────────────────────────────

def test_every_tool_is_dispatchable_guardrailed_and_grouped():
    import inspect

    import backend.action_parser as action_parser
    # Two dispatch routes, both legitimate: an explicit branch in
    # dispatch_tool_call, or the generic fall-through to execute_action, which
    # is how the task/email/meeting actions are handled.
    src = (inspect.getsource(tools.dispatch_tool_call)
           + inspect.getsource(action_parser.execute_action))
    for schema in tools.TOOL_SCHEMAS:
        name = schema["function"]["name"]
        assert guardrails.decide(name) != "deny", \
            f"{name} is offered to the model but denied at dispatch"
        assert name in tools.ACTION_TOOLS or name in tools.READ_TOOLS, \
            f"{name} is classified as neither an action nor a read tool"
        assert schema.get("group") in tools.GROUP_IDS, \
            f"{name} has no valid group — it would be untoggleable"
        assert f'"{name}"' in src, f"{name} has no dispatch branch"


def test_every_group_has_at_least_one_tool():
    """An empty group is a dead switch in the UI."""
    used = {s.get("group") for s in tools.TOOL_SCHEMAS}
    for g in tools.TOOL_GROUPS:
        assert g["id"] in used, f"group {g['id']!r} has no tools"


def test_group_ids_and_registry_agree():
    assert tools.GROUP_IDS == {g["id"] for g in tools.TOOL_GROUPS}


def test_every_group_declares_user_visible():
    """Required, like `group` itself — a new group must DECIDE whether it belongs
    in the panel rather than drifting in by default."""
    for g in tools.TOOL_GROUPS:
        assert isinstance(g.get("user_visible"), bool), \
            f"group {g['id']!r} must declare user_visible explicitly"


# ── panel visibility (presentation only) ─────────────────────────────────────

def test_panel_hides_the_two_quiet_groups():
    ids = {g["id"] for g in tools.visible_groups([])}
    assert "memory" not in ids, "memory degrades quietly instead of failing visibly"
    assert "knowledge" not in ids, "the group's label does not name a user concept"
    # Derived, not a literal: this used to assert `== 9` and broke the moment a
    # new group shipped, which teaches people to bump the number rather than ask
    # whether the hiding rule still holds. The property is "exactly the visible
    # ones show", and that stays true at any group count.
    expected = {g["id"] for g in tools.TOOL_GROUPS if g["user_visible"]}
    assert ids == expected
    assert len(ids) == len(tools.TOOL_GROUPS) - 2, "exactly two groups are hidden"


def test_contacts_stays_visible_and_states_its_dependency():
    """Turning it off makes Email look broken; the label has to say so."""
    g = next(g for g in tools.TOOL_GROUPS if g["id"] == "contacts")
    assert g["user_visible"] is True
    assert "Email" in g["label"] and "Calendar" in g["label"]


def test_a_hidden_group_that_is_disabled_is_always_reachable():
    """The trapped state, ruled out by construction: a hidden group is only ever
    absent from the panel while it is ON."""
    for hidden in (g["id"] for g in tools.TOOL_GROUPS if not g["user_visible"]):
        assert hidden not in {g["id"] for g in tools.visible_groups([])}
        assert hidden in {g["id"] for g in tools.visible_groups([hidden])}, \
            f"{hidden} would be stranded off with no way back"


def test_re_enabling_a_hidden_group_hides_it_again():
    assert "memory" in {g["id"] for g in tools.visible_groups(["memory"])}
    assert "memory" not in {g["id"] for g in tools.visible_groups([])}


def test_visibility_does_not_change_filtering():
    """PRESENTATION ONLY. A hidden group must still filter correctly when off,
    or hiding it would quietly grant tools back."""
    offered = {s["function"]["name"] for s in tools.tools_for(["memory"])}
    assert "recall_memory" not in offered and "remember_fact" not in offered
    assert "get_emails" in offered
    # and the dispatch rejection is unaffected by visibility
    assert tools.group_of("recall_memory") == "memory"


# ── 2. default reachability ──────────────────────────────────────────────────

def test_with_no_preferences_every_tool_reaches_the_model():
    offered = {s["function"]["name"] for s in tools.tools_for([])}
    expected = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert offered == expected, "absent preferences must mean everything enabled"


def test_a_new_tool_is_enabled_without_touching_stored_prefs():
    """Preferences store the DISABLED list. If they stored the enabled one, every
    existing user would be silently withheld from every tool we ship next."""
    offered = {s["function"]["name"] for s in tools.tools_for(["email"])}
    assert "get_weather" in offered


def test_unknown_group_in_a_stale_pref_disables_nothing():
    assert len(tools.tools_for(["a_group_we_renamed"])) == len(tools.TOOL_SCHEMAS)


# ── 3. filter honesty ────────────────────────────────────────────────────────

def test_a_disabled_group_is_absent_from_the_payload():
    """Absent, not refused. Refusing at dispatch would let the model call a tool
    the user switched off, fail, and apologise for it."""
    offered = {s["function"]["name"] for s in tools.tools_for(["email"])}
    for name in ("get_emails", "read_email", "draft_email"):
        assert name not in offered
    assert "get_agenda" in offered, "only the named group may be removed"


def test_disabling_every_group_leaves_no_tools():
    assert tools.tools_for(list(tools.GROUP_IDS)) == []


def test_group_is_stripped_before_the_wire():
    """`group` is ours. The provider gets OpenAI's shape and nothing else."""
    for s in tools.tools_for([]):
        assert "group" not in s
        assert set(s) == {"type", "function"}


def test_toggles_subtract_only_and_never_grant():
    """A toggle may hide a tool guardrails allow; it may never expose one they
    deny. Guardrails run first at dispatch and are not consulted by the filter."""
    import inspect
    src = inspect.getsource(tools.tools_for)
    assert "guardrail" not in src.lower() and "decide" not in src, \
        "the group filter must not consult guardrails — it can only subtract"
    assert guardrails.decide("a_tool_that_does_not_exist") == "deny"
