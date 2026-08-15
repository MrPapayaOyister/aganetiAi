"""Level 2 (§13) — the semantic tool layer. No LLM, no registry, no authorization.

Level 1 proved the worker is correct. This proves the layer above it is:

  * the schemas are the contract, and cannot grow a selector channel;
  * arguments are rejected before the worker is called;
  * a worker error becomes a result an agent can act on;
  * **a validation rejection does not read as success** — the single most important
    property here, because that is the one where a wrong answer causes an agent to
    submit a form the server already refused;
  * nothing §7.2 forbids survives into a result.

Most tests need neither Chromium nor the lab: the gateway is faked, so the mapping
is provable on any machine. The handful that drive the real worker end to end carry
the `browser`/`lab` markers from `tests/conftest.py`.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import pytest_asyncio

import browser_tools as bt
from browser_tools import artifacts as art
from browser_tools.outcomes import (
    CORRECTABLE, RETRYABLE, TERMINAL, Outcome, assert_taxonomy_complete,
    is_terminal, recovery_for,
)
from browser_tools.results import _scrub, from_observation
from browser_tools.schemas import FORBIDDEN_PARAMS, SCHEMAS, TOOL_NAMES
from browser_tools.tools import TOOLS

OWNER = dict(tenant_id="tenant-a", user_id="user-alice",
             agent_id="browser_agent", session_id="task-1")

# The lab's committed fake credential (§10.4). Used as the canary in the leak scan:
# if this string survives into a serialized result, a real one would too.
FIXTURE_PASSWORD = "not-a-real-password"
FIXTURE_EMAIL = "demo@browser-lab.invalid"


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeGateway:
    """Records what reached the worker and returns a scripted observation.

    A fake rather than a mock: the tests assert on the payload the gateway was
    handed, which is the actual contract between this layer and the worker.
    """

    def __init__(self, observation: dict | None = None):
        self.calls: list[dict] = []
        self.screenshot_calls: list[tuple] = []
        self.observation = observation or {"ok": True, "action": "x"}
        self.png = b"\x89PNG\r\n\x1a\n" + b"fake image bytes" * 8

    async def call(self, **kw) -> dict:
        self.calls.append(kw)
        obs = dict(self.observation)
        obs.setdefault("action", kw.get("action", ""))
        return obs

    async def fetch_screenshot(self, ref, *, tenant_id, user_id) -> bytes:
        self.screenshot_calls.append((ref, tenant_id, user_id))
        return self.png


@pytest.fixture
def gw():
    return FakeGateway()


@pytest.fixture
def store(tmp_path):
    return art.FilesystemArtifactStore(tmp_path / "artifacts")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Schemas — the model-facing contract
# ══════════════════════════════════════════════════════════════════════════════
class TestSchemas:
    def test_there_are_exactly_fourteen(self):
        assert len(SCHEMAS) == 14
        assert len(TOOLS) == 14
        assert set(SCHEMAS) == set(TOOLS)

    def test_names_match_the_workers_closed_enum(self):
        from playwright_worker.protocol import Action
        assert set(SCHEMAS) == {a.value for a in Action}

    def test_names_are_snake_case(self):
        """§3.1: dotted names would be the first inconsistency in the registry."""
        for name in SCHEMAS:
            assert name.islower() and "." not in name and " " not in name
            assert name.startswith("browser_")

    @pytest.mark.parametrize("name", sorted(SCHEMAS))
    def test_every_schema_is_structurally_valid(self, name):
        s = SCHEMAS[name]
        assert s["name"] == name
        assert len(s["description"]) > 60, "too thin for a model to route on"
        p = s["parameters"]
        assert p["type"] == "object"
        assert isinstance(p["properties"], dict)
        assert p["additionalProperties"] is False, "unknown args must be refused"
        for prop, spec in p["properties"].items():
            assert "type" in spec, f"{name}.{prop} has no type"
            assert "description" in spec, f"{name}.{prop} has no description"
        for r in p["required"]:
            assert r in p["properties"], f"{name} requires undeclared {r}"

    @pytest.mark.parametrize("name", sorted(SCHEMAS))
    def test_ownership_is_explicit_on_every_tool(self, name):
        """Requirement 2: ownership is a parameter, never ambient."""
        p = SCHEMAS[name]["parameters"]
        for f in ("tenant_id", "user_id", "agent_id", "session_id"):
            assert f in p["properties"], f"{name} has no {f}"
            assert f in p["required"], f"{name} does not require {f}"

    @pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_PARAMS))
    def test_no_schema_offers_a_selector_code_or_path_channel(self, forbidden):
        """§3.3 / §11 S2/S3, walked over every schema so a fifteenth tool cannot
        add one quietly."""
        for name, s in SCHEMAS.items():
            assert forbidden not in s["parameters"]["properties"], \
                f"{name} declares {forbidden!r}"

    def test_element_ref_is_the_only_addressing_mode(self):
        addressing = set()
        for s in SCHEMAS.values():
            addressing |= {k for k in s["parameters"]["properties"]
                           if k not in ("tenant_id", "user_id", "agent_id", "session_id",
                                        "browser_session_id")}
        # Every remaining parameter is either element_ref or plain data — no other
        # way to point at something on the page.
        assert "element_ref" in addressing
        assert not (addressing & FORBIDDEN_PARAMS)

    def test_element_ref_carries_a_pattern_the_model_can_read(self):
        for name, s in SCHEMAS.items():
            spec = s["parameters"]["properties"].get("element_ref")
            if spec:
                assert spec.get("pattern") == r"^e[0-9]{1,6}$", name

    def test_no_schema_declares_a_risk_level(self):
        """§3.2: risk is derived from TOOL_CATEGORY, never author-supplied. Writing
        one here would repeat the category error the design already corrected."""
        for name, s in SCHEMAS.items():
            assert "risk_level" not in s and "risk" not in s["parameters"]["properties"], name

    def test_openai_shape_round_trips(self):
        fns = bt.openai_schemas()
        assert len(fns) == 14
        assert all(f["type"] == "function" for f in fns)
        assert {f["function"]["name"] for f in fns} == set(SCHEMAS)
        json.loads(json.dumps(fns))   # serializable as-is

    def test_a_forbidden_param_fails_at_import_not_only_in_ci(self):
        """The import-time guard is the second lock: a test proves it in CI, the
        guard proves it in every process that loads the package."""
        from browser_tools import schemas as S
        original = S.SCHEMAS["browser_click"]["parameters"]["properties"]
        S.SCHEMAS["browser_click"]["parameters"]["properties"] = {**original, "selector": {"type": "string"}}
        try:
            with pytest.raises(ImportError, match="forbidden parameter"):
                S._verify_no_forbidden_params()
        finally:
            S.SCHEMAS["browser_click"]["parameters"]["properties"] = original


# ══════════════════════════════════════════════════════════════════════════════
# 2. Mapping: tool -> worker action
# ══════════════════════════════════════════════════════════════════════════════
class TestActionMapping:
    @pytest.mark.parametrize("name", sorted(SCHEMAS))
    def test_each_tool_maps_to_its_own_worker_action(self, name, gw):
        args = {"browser_session_id": "bs_x"} if name != "browser_open" else {}
        for extra, val in (("element_ref", "e1"), ("url", "http://x.test/"),
                           ("value", "v"), ("artifact_id", "art-1"),
                           ("condition", "load")):
            if extra in SCHEMAS[name]["parameters"]["required"]:
                args[extra] = val
        _await(TOOLS[name](**OWNER, gateway=gw, **args))
        assert gw.calls, f"{name} never reached the gateway"
        assert gw.calls[-1]["action"] == name

    def test_ownership_is_forwarded_verbatim(self, gw):
        _await(TOOLS["browser_inspect"](**OWNER, browser_session_id="bs_x", gateway=gw))
        c = gw.calls[-1]
        for k, v in OWNER.items():
            assert c[k] == v, f"{k} was not forwarded"

    def test_the_gateway_is_the_only_route_to_the_worker(self):
        """Requirement 6. If a tool could reach the worker another way, Phase E's
        anti-bypass test would be unprovable."""
        import inspect

        from browser_tools import tools as T
        src = inspect.getsource(T)
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        for forbidden in ("BrowserWorker(", "httpx.", "InProcessTransport(",
                          "HttpTransport(", ".execute("):
            assert forbidden not in code, f"tools.py reaches the worker via {forbidden!r}"
        # Everything goes through _run or the one documented exception, and both
        # call the gateway.
        assert "gw.call(" in code or "gateway.call(" in code

    def test_phase_e_has_exactly_one_hook(self):
        import inspect

        from browser_tools import client as C
        assert hasattr(C.WorkerGateway, "_authorize")
        assert "self._authorize(" in inspect.getsource(C.WorkerGateway.call)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Validation before the worker
# ══════════════════════════════════════════════════════════════════════════════
class TestValidationBeforeWorker:
    def test_missing_ownership_is_refused_without_a_call(self, gw):
        res = _await(TOOLS["browser_inspect"](
            tenant_id="", user_id="u", agent_id="a", session_id="s",
            browser_session_id="bs_x", gateway=gw))
        assert res.outcome is Outcome.FAILED
        assert res.error_code == "BAD_REQUEST"
        assert "tenant_id" in res.error_message
        assert gw.calls == [], "the worker was called for a call that could not succeed"

    def test_unknown_argument_is_refused_without_a_call(self, gw):
        res = _await(TOOLS["browser_inspect"](
            **OWNER, browser_session_id="bs_x", gateway=gw, depth=3))
        assert res.error_code == "BAD_REQUEST"
        assert "depth" in res.error_message
        assert gw.calls == []

    def test_missing_required_argument_is_refused_without_a_call(self, gw):
        res = _await(TOOLS["browser_navigate"](
            **OWNER, browser_session_id="bs_x", gateway=gw))
        assert res.error_code == "BAD_REQUEST"
        assert gw.calls == []

    @pytest.mark.parametrize("bad", [17, None, ["e1"], {"r": "e1"}, "", "E1", "elem1"])
    def test_bad_element_ref_is_refused_without_a_call(self, gw, bad):
        res = _await(TOOLS["browser_click"](
            **OWNER, browser_session_id="bs_x", element_ref=bad, gateway=gw))
        assert res.outcome is Outcome.FAILED
        assert res.error_code in ("BAD_REQUEST", "SELECTOR_REJECTED")
        assert gw.calls == []

    def test_wrong_types_are_refused_without_a_call(self, gw):
        cases = [
            ("browser_fill", dict(element_ref="e1", value=17)),
            ("browser_check", dict(element_ref="e1", checked="yes")),
            ("browser_select", dict(element_ref="e1", value="")),
            ("browser_inspect", dict(max_elements=0)),
            ("browser_wait", dict(condition="whenever")),
            ("browser_wait", dict(condition="element_visible")),   # needs element_ref
            ("browser_extract", dict(scope="everything")),
            ("browser_screenshot", dict(full_page="yes")),
            ("browser_open", dict(allowed_domains="example.com")),
        ]
        for tool, kwargs in cases:
            gw.calls.clear()
            res = _await(TOOLS[tool](**OWNER, browser_session_id="bs_x",
                                     gateway=gw, **kwargs))
            assert res.outcome is Outcome.FAILED, f"{tool} {kwargs}"
            assert res.error_code == "BAD_REQUEST", f"{tool} {kwargs}"
            assert gw.calls == [], f"{tool} reached the worker with {kwargs}"

    def test_a_path_shaped_artifact_id_is_refused(self, gw):
        """§11.4: the schema has no `path`, so this is the remaining way one could
        arrive."""
        for bad in ("../../etc/passwd", "/etc/passwd", "./secret", "a\\b"):
            gw.calls.clear()
            res = _await(TOOLS["browser_upload"](
                **OWNER, browser_session_id="bs_x", element_ref="e1",
                artifact_id=bad, gateway=gw))
            assert res.error_code == "SELECTOR_REJECTED", bad
            assert gw.calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 4. Selector rejection at BOTH layers, independently
# ══════════════════════════════════════════════════════════════════════════════
SELECTORS = [
    "#app-submit", ".btn.primary", "button[type=submit]", "div > button",
    "//button[@type='submit']", "(//input)[1]", "../button",
    "css=button", "xpath=//button", "text=Submit", "data-testid=app-submit",
    "role=button[name='x']", "input:nth-child(2)", "*", "form button, form input",
]


class TestSelectorRejectionAtBothLayers:
    @pytest.mark.parametrize("hostile", SELECTORS)
    def test_tool_layer_refuses_without_calling_the_worker(self, gw, hostile):
        res = _await(TOOLS["browser_click"](
            **OWNER, browser_session_id="bs_x", element_ref=hostile, gateway=gw))
        assert res.error_code == "SELECTOR_REJECTED", hostile
        assert gw.calls == [], f"{hostile!r} reached the worker"

    @pytest.mark.parametrize("hostile", SELECTORS)
    def test_worker_layer_refuses_the_same_input_independently(self, hostile):
        """The two layers must not share one check. If the tool layer's regex were
        deleted tomorrow, the worker must still refuse — and vice versa."""
        from playwright_worker.errors import ErrorCode, WorkerError
        from playwright_worker.protocol import validate_element_ref
        with pytest.raises(WorkerError) as ei:
            validate_element_ref(hostile)
        assert ei.value.code is ErrorCode.SELECTOR_REJECTED, hostile

    def test_a_forbidden_parameter_name_is_refused_by_the_tool_layer(self, gw):
        res = _await(TOOLS["browser_click"](
            **OWNER, browser_session_id="bs_x", element_ref="e1",
            gateway=gw, selector="#x"))
        assert res.error_code == "SELECTOR_REJECTED"
        assert gw.calls == []

    def test_the_two_layers_do_not_share_an_implementation(self):
        """A shared constant would be one layer with an extra function call."""
        import inspect

        from browser_tools import tools as T
        src = inspect.getsource(T)
        assert "from playwright_worker" not in src or "validate_element_ref" not in src, \
            "the tool layer delegates its refusal to the worker's function"


# ══════════════════════════════════════════════════════════════════════════════
# 5. VALIDATION_ERROR must not read as success
# ══════════════════════════════════════════════════════════════════════════════
VALIDATION_OBS = {
    "ok": True,                      # the transport says True — deliberately
    "action": "browser_submit",
    "url": "http://lab/application",
    "title": "Application",
    "error": "VALIDATION_ERROR",
    "error_message": "phone: That telephone number was not accepted.",
    "error_detail": {"messages": ["phone: That telephone number was not accepted."]},
}


class TestValidationIsNotSuccess:
    def test_transport_says_ok_but_the_tool_layer_does_not(self, gw):
        gw.observation = VALIDATION_OBS
        res = _await(TOOLS["browser_submit"](
            **OWNER, browser_session_id="bs_x", element_ref="e19", gateway=gw))
        assert gw.observation["ok"] is True, "fixture no longer exercises the asymmetry"
        assert res.ok is False, "a rejected form read as success"
        assert res.outcome is Outcome.NEEDS_CORRECTION
        assert res.outcome is not Outcome.OK

    def test_it_is_also_not_a_plain_failure(self, gw):
        gw.observation = VALIDATION_OBS
        res = _await(TOOLS["browser_submit"](
            **OWNER, browser_session_id="bs_x", element_ref="e19", gateway=gw))
        assert res.outcome is not Outcome.FAILED
        assert res.terminal is False

    def test_the_field_is_named(self, gw):
        """§9.1's recovery is 'correct the field', which requires knowing which."""
        gw.observation = VALIDATION_OBS
        res = _await(TOOLS["browser_submit"](
            **OWNER, browser_session_id="bs_x", element_ref="e19", gateway=gw))
        assert "phone" in res.field_errors
        assert "not accepted" in res.field_errors["phone"]

    def test_the_recovery_is_stated(self, gw):
        gw.observation = VALIDATION_OBS
        res = _await(TOOLS["browser_submit"](
            **OWNER, browser_session_id="bs_x", element_ref="e19", gateway=gw))
        assert "correct" in res.recovery.lower()
        assert "not a failure" in res.recovery.lower()

    def test_the_rendering_cannot_be_mistaken_for_done(self, gw):
        gw.observation = VALIDATION_OBS
        res = _await(TOOLS["browser_submit"](
            **OWNER, browser_session_id="bs_x", element_ref="e19", gateway=gw))
        text = res.for_model()
        assert text.startswith("NEEDS CORRECTION")
        assert "phone" in text
        low = text.lower()
        assert "submitted" not in low and "done" not in low

    def test_unparsed_messages_are_still_surfaced(self, gw):
        gw.observation = {**VALIDATION_OBS,
                          "error_detail": {"messages": ["Something was wrong."]}}
        res = _await(TOOLS["browser_submit"](
            **OWNER, browser_session_id="bs_x", element_ref="e19", gateway=gw))
        assert res.field_errors, "a complaint we could not parse was dropped"
        assert "Something was wrong." in " ".join(res.field_errors.values())


# ══════════════════════════════════════════════════════════════════════════════
# 6. The four worker-boundary codes are terminal
# ══════════════════════════════════════════════════════════════════════════════
BOUNDARY = ["BAD_REQUEST", "SELECTOR_REJECTED", "SESSION_NOT_FOUND", "BUDGET_EXCEEDED"]


class TestBoundaryCodesAreTerminal:
    @pytest.mark.parametrize("code", BOUNDARY)
    def test_marked_terminal(self, code):
        assert code in TERMINAL
        assert is_terminal(code)

    @pytest.mark.parametrize("code", BOUNDARY)
    def test_never_retryable(self, code):
        assert code not in RETRYABLE
        assert code not in CORRECTABLE

    @pytest.mark.parametrize("code", BOUNDARY)
    def test_recovery_never_says_retry(self, code):
        r = recovery_for(code).lower()
        assert r, f"{code} has no documented recovery"
        assert "retry" not in r or "retrying" in r, \
            f"{code} recovery invites a retry: {r!r}"
        assert any(w in r for w in ("stop", "fix")), f"{code}: {r!r}"

    @pytest.mark.parametrize("code", BOUNDARY)
    def test_surfaces_as_terminal_on_a_result(self, gw, code):
        gw.observation = {"ok": False, "action": "browser_click", "error": code,
                          "error_message": "refused"}
        res = _await(TOOLS["browser_click"](
            **OWNER, browser_session_id="bs_x", element_ref="e1", gateway=gw))
        assert res.outcome is Outcome.FAILED
        assert res.terminal is True, code

    def test_the_taxonomy_has_no_gaps_or_overlaps(self):
        assert_taxonomy_complete()

    def test_origin_matches_the_workers_vocabulary(self):
        from playwright_worker.errors import Origin as WorkerOrigin

        from browser_tools.outcomes import Origin
        assert {o.value for o in Origin} == {o.value for o in WorkerOrigin}

    def test_a_pre_worker_refusal_is_stamped_tool(self, gw):
        """BAD_REQUEST/tool and BAD_REQUEST/worker mean the same thing about the
        call and different things about whether validation is working. §11.6 needs
        both, and the code alone cannot say."""
        from browser_tools.outcomes import Origin
        res = _await(TOOLS["browser_inspect"](
            **OWNER, browser_session_id="bs_x", gateway=gw, depth=3))
        assert res.error_code == "BAD_REQUEST"
        assert res.origin is Origin.TOOL
        assert res.as_dict()["origin"] == "tool"
        assert gw.calls == []

    def test_a_worker_refusal_is_stamped_worker(self, gw):
        from browser_tools.outcomes import Origin
        gw.observation = {"ok": False, "action": "browser_click",
                          "error": "BAD_REQUEST", "error_message": "malformed",
                          "origin": "worker"}
        res = _await(TOOLS["browser_click"](
            **OWNER, browser_session_id="bs_x", element_ref="e1", gateway=gw))
        assert res.error_code == "BAD_REQUEST"
        assert res.origin is Origin.WORKER

    def test_selector_rejection_records_which_layer_caught_it(self, gw):
        """Both layers refuse the same input with the same code. `origin` is the
        only thing that says which one did — and therefore the only way to notice
        the tool layer's check silently stopping working."""
        from browser_tools.outcomes import Origin
        tool_side = _await(TOOLS["browser_click"](
            **OWNER, browser_session_id="bs_x", element_ref="#submit", gateway=gw))
        assert tool_side.error_code == "SELECTOR_REJECTED"
        assert tool_side.origin is Origin.TOOL
        assert gw.calls == []

        gw.observation = {"ok": False, "action": "browser_click",
                          "error": "SELECTOR_REJECTED", "error_message": "refused",
                          "origin": "worker"}
        worker_side = _await(TOOLS["browser_click"](
            **OWNER, browser_session_id="bs_x", element_ref="e1", gateway=gw))
        assert worker_side.error_code == "SELECTOR_REJECTED"
        assert worker_side.origin is Origin.WORKER

    def test_ninepointone_codes_keep_their_documented_recovery(self):
        assert "re-inspect" in recovery_for("STALE_REF").lower()
        assert "does not count" in recovery_for("STALE_REF").lower()
        assert "never retry" in recovery_for("DOMAIN_DENIED").lower()
        assert "escalation" in recovery_for("AUTHZ_DENIED").lower()


# ══════════════════════════════════════════════════════════════════════════════
# 7. Submit refusal and stale refs surface usefully
# ══════════════════════════════════════════════════════════════════════════════
class TestRefusalsAreUsable:
    def test_click_on_a_submit_points_at_browser_submit(self, gw):
        gw.observation = {
            "ok": False, "action": "browser_click", "error": "WRONG_TOOL_FOR_SUBMIT",
            "error_message": ("e19 (button/submit 'Submit application') is a submit "
                              "control. browser_click does not submit forms — use "
                              "browser_submit, which is approval-gated."),
            "error_detail": {"use_instead": "browser_submit", "element_ref": "e19"},
        }
        res = _await(TOOLS["browser_click"](
            **OWNER, browser_session_id="bs_x", element_ref="e19", gateway=gw))
        assert res.outcome is Outcome.FAILED
        assert res.error_code == "WRONG_TOOL_FOR_SUBMIT"
        assert "browser_submit" in res.recovery
        assert res.detail["use_instead"] == "browser_submit"
        # Actionable, not a dead end. This is now a property of the CODE rather
        # than of a rewrite in the click handler, so a §11.6 audit row carries the
        # right meaning too.
        assert res.terminal is False
        assert "browser_submit" in res.for_model()

    def test_a_routing_correction_is_not_an_escalation_attempt(self, gw):
        """The reason the code had to be split: these are opposite behaviours and
        a §11.6 audit row must be able to tell them apart."""
        from browser_tools.outcomes import TERMINAL, recovery_for
        assert "WRONG_TOOL_FOR_SUBMIT" not in TERMINAL
        assert "AUTHZ_DENIED" in TERMINAL
        assert "escalation" in recovery_for("AUTHZ_DENIED").lower()
        assert "escalation" not in recovery_for("WRONG_TOOL_FOR_SUBMIT").lower()
        assert "browser_submit" in recovery_for("WRONG_TOOL_FOR_SUBMIT")

    def test_a_genuine_authz_denial_stays_terminal(self, gw):
        gw.observation = {"ok": False, "action": "browser_click", "error": "AUTHZ_DENIED",
                          "error_message": "not permitted", "error_detail": {}}
        res = _await(TOOLS["browser_click"](
            **OWNER, browser_session_id="bs_x", element_ref="e1", gateway=gw))
        assert res.terminal is True
        assert "escalation" in res.recovery.lower()

    def test_stale_ref_recovery_is_reinspect(self, gw):
        gw.observation = {"ok": False, "action": "browser_fill", "error": "STALE_REF",
                          "error_message": "e5 no longer resolves — re-inspect",
                          "error_detail": {"element_ref": "e5"}}
        res = _await(TOOLS["browser_fill"](
            **OWNER, browser_session_id="bs_x", element_ref="e5",
            value="x", gateway=gw))
        assert res.error_code == "STALE_REF"
        assert res.terminal is False
        assert "re-inspect" in res.recovery.lower()
        assert "does not count against element retries" in res.recovery.lower()
        assert "browser_inspect" in res.recovery

    def test_submit_on_a_non_submit_points_back_at_click(self, gw):
        gw.observation = {"ok": False, "action": "browser_submit", "error": "BAD_REQUEST",
                          "error_message": "not a submit control",
                          "error_detail": {"use_instead": "browser_click"}}
        res = _await(TOOLS["browser_submit"](
            **OWNER, browser_session_id="bs_x", element_ref="e5", gateway=gw))
        assert "browser_click" in res.recovery

    def test_close_is_idempotent_at_the_tool_layer(self, gw):
        gw.observation = {"ok": False, "action": "browser_close",
                          "error": "SESSION_NOT_FOUND", "error_message": "no such session"}
        res = _await(TOOLS["browser_close"](
            **OWNER, browser_session_id="bs_gone", gateway=gw))
        assert res.ok, "the schema promises idempotence"
        assert "already closed" in res.summary


# ══════════════════════════════════════════════════════════════════════════════
# 8. §7.2 — what must never enter state
# ══════════════════════════════════════════════════════════════════════════════
class TestRedaction:
    def test_a_sensitive_fill_records_that_not_what(self, gw):
        gw.observation = {"ok": True, "action": "browser_fill",
                          "extracted": {"sensitive": True, "value": None}}
        res = _await(TOOLS["browser_fill"](
            **OWNER, browser_session_id="bs_x", element_ref="e6",
            value=FIXTURE_PASSWORD, sensitive=True, gateway=gw))
        assert res.detail["sensitive"] is True
        assert res.detail["value"] is None
        assert res.detail["value_recorded"] is False
        blob = json.dumps(res.as_dict())
        assert FIXTURE_PASSWORD not in blob
        assert FIXTURE_PASSWORD not in res.for_model()

    def test_the_worker_classification_wins_even_if_the_model_did_not_flag_it(self, gw):
        """A page-marked password field is protected whether or not the model said so
        — which is the case that actually matters, because the model is the thing
        most likely to forget."""
        gw.observation = {"ok": True, "action": "browser_fill",
                          "extracted": {"sensitive": True}}
        res = _await(TOOLS["browser_fill"](
            **OWNER, browser_session_id="bs_x", element_ref="e6",
            value=FIXTURE_PASSWORD, gateway=gw))    # sensitive NOT passed
        assert res.detail["sensitive"] is True
        assert res.detail["value"] is None
        assert FIXTURE_PASSWORD not in json.dumps(res.as_dict())

    def test_a_non_sensitive_value_is_kept(self, gw):
        gw.observation = {"ok": True, "action": "browser_fill", "extracted": {}}
        res = _await(TOOLS["browser_fill"](
            **OWNER, browser_session_id="bs_x", element_ref="e5",
            value="Ada Lovelace", gateway=gw))
        assert res.detail["value"] == "Ada Lovelace"

    @pytest.mark.parametrize("key", [
        "cookie", "cookies", "storage_state", "storageState", "token",
        "password", "secret", "raw_html", "inner_html", "dom", "png", "base64",
    ])
    def test_forbidden_keys_are_scrubbed_from_detail(self, key):
        out = _scrub({key: "leaked", "nested": {key: "also leaked"}})
        assert out[key] == "[redacted]"
        assert out["nested"][key] == "[redacted]"

    def test_inline_images_are_refused_whatever_they_are_called(self):
        out = _scrub({"evidence": "data:image/png;base64,iVBORw0KGgoAAAA",
                      "other": "iVBORw0KGgoAAAANSUhEUg"})
        assert out["evidence"] == "[redacted]"
        assert out["other"] == "[redacted]"

    def test_a_result_has_no_field_that_could_carry_bytes(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(bt.ToolResult)}
        assert not (names & {"png", "image", "screenshot", "bytes", "content",
                             "html", "dom", "cookies"})
        assert "artifact_ref" in names, "the reference is the only image channel"

    def test_full_session_serialization_leaks_nothing(self, gw, store):
        """Required: serialize a whole session's results and scan for the fixture
        credential, a cookie value and a raw image."""
        cookie_value = "sess-1-abcdef-supersecret-cookie"
        image_marker = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"

        results = []
        gw.observation = {"ok": True, "action": "browser_open"}
        results.append(_await(TOOLS["browser_open"](**OWNER, gateway=gw)))

        gw.observation = {"ok": True, "action": "browser_navigate",
                          "url": "http://lab/login", "title": "Sign in"}
        results.append(_await(TOOLS["browser_navigate"](
            **OWNER, browser_session_id="bs_x", url="http://lab/login", gateway=gw)))

        gw.observation = {
            "ok": True, "action": "browser_inspect", "url": "http://lab/login",
            "element_total": 2, "elements": [
                {"ref": "e5", "role": "textbox", "accessible_name": "Email address",
                 "type": "email", "state": {}},
                {"ref": "e6", "role": "textbox", "accessible_name": "Pass phrase",
                 "type": "password", "state": {}},
            ],
            # Hostile extras a careless worker or page could attach.
            "cookies": cookie_value, "storage_state": {"cookies": [cookie_value]},
            "raw_html": "<html><body>secret</body></html>",
        }
        results.append(_await(TOOLS["browser_inspect"](
            **OWNER, browser_session_id="bs_x", gateway=gw)))

        gw.observation = {"ok": True, "action": "browser_fill",
                          "extracted": {"sensitive": False}}
        results.append(_await(TOOLS["browser_fill"](
            **OWNER, browser_session_id="bs_x", element_ref="e5",
            value=FIXTURE_EMAIL, gateway=gw)))

        gw.observation = {"ok": True, "action": "browser_fill",
                          "extracted": {"sensitive": True}}
        results.append(_await(TOOLS["browser_fill"](
            **OWNER, browser_session_id="bs_x", element_ref="e6",
            value=FIXTURE_PASSWORD, sensitive=True, gateway=gw)))

        gw.observation = {"ok": True, "action": "browser_screenshot",
                          "url": "http://lab/login", "screenshot_ref": "shot_vol",
                          "extracted": {"masked_fields": 2}}
        gw.png = bytes.fromhex("89504e470d0a1a0a") + b"raw image data" * 20
        results.append(_await(TOOLS["browser_screenshot"](
            **OWNER, browser_session_id="bs_x", gateway=gw, store=store)))

        blob = json.dumps([r.as_dict() for r in results])
        rendered = "\n".join(r.for_model() for r in results)

        assert FIXTURE_PASSWORD not in blob, "the fixture credential leaked"
        assert FIXTURE_PASSWORD not in rendered
        assert cookie_value not in blob, "a cookie value leaked"
        assert cookie_value not in rendered
        assert image_marker not in blob, "a raw image leaked"
        assert "raw image data" not in blob
        assert "<html>" not in blob and "<body>" not in blob, "raw DOM leaked"
        # The non-sensitive email IS expected — it is form data the agent must be
        # able to reason about. Asserting it survives proves the scan is not simply
        # deleting everything.
        assert FIXTURE_EMAIL in blob


# ══════════════════════════════════════════════════════════════════════════════
# 9. Screenshot durability (requirement 5)
# ══════════════════════════════════════════════════════════════════════════════
class TestScreenshotDurability:
    def test_returns_a_durable_ref_not_the_worker_ref(self, gw, store):
        gw.observation = {"ok": True, "action": "browser_screenshot",
                          "url": "http://lab/x", "screenshot_ref": "shot_volatile",
                          "extracted": {"masked_fields": 1}}
        res = _await(TOOLS["browser_screenshot"](
            **OWNER, browser_session_id="bs_x", gateway=gw, store=store))
        assert res.ok
        assert res.artifact_ref and res.artifact_ref.startswith("art_")
        assert res.artifact_ref != "shot_volatile"
        assert "shot_volatile" not in json.dumps(res.as_dict()), \
            "the volatile worker ref travelled onward"

    def test_the_bytes_are_persisted_and_readable(self, gw, store):
        gw.observation = {"ok": True, "action": "browser_screenshot",
                          "url": "http://lab/x", "screenshot_ref": "shot_v",
                          "extracted": {"masked_fields": 0}}
        res = _await(TOOLS["browser_screenshot"](
            **OWNER, browser_session_id="bs_x", gateway=gw, store=store))
        data, state = bt.fetch_screenshot(res.artifact_ref, tenant_id=OWNER["tenant_id"],
                                          user_id=OWNER["user_id"], store=store)
        assert state is art.ArtifactState.AVAILABLE
        assert data == gw.png

    def test_the_reference_survives_a_worker_restart(self, gw, tmp_path):
        """§8.2(c): the payload is reviewed minutes to hours later. A process-memory
        ref would be a broken image at exactly the moment a human is deciding."""
        store_a = art.FilesystemArtifactStore(tmp_path / "arts")
        gw.observation = {"ok": True, "action": "browser_screenshot",
                          "url": "http://lab/x", "screenshot_ref": "shot_v",
                          "extracted": {"masked_fields": 0}}
        res = _await(TOOLS["browser_screenshot"](
            **OWNER, browser_session_id="bs_x", gateway=gw, store=store_a))

        # A brand-new store object over the same directory == a restarted process.
        store_b = art.FilesystemArtifactStore(tmp_path / "arts")
        data, state = bt.fetch_screenshot(res.artifact_ref, tenant_id=OWNER["tenant_id"],
                                          user_id=OWNER["user_id"], store=store_b)
        assert state is art.ArtifactState.AVAILABLE
        assert data == gw.png

    def test_missing_is_distinguishable_from_expired(self, store):
        """The requirement, and the reason the manifest outlives the bytes."""
        ref = store.put(data=b"png-bytes", kind="browser_screenshot",
                        content_type="image/png", tenant_id=OWNER["tenant_id"],
                        user_id=OWNER["user_id"], ttl_s=0)
        expired = store.state(ref.artifact_id, tenant_id=OWNER["tenant_id"],
                              user_id=OWNER["user_id"])
        missing = store.state("art_neverexisted", tenant_id=OWNER["tenant_id"],
                              user_id=OWNER["user_id"])
        assert expired is art.ArtifactState.EXPIRED
        assert missing is art.ArtifactState.MISSING
        assert expired != missing

    def test_reaping_bytes_keeps_the_manifest(self, store):
        ref = store.put(data=b"x" * 100, kind="browser_screenshot",
                        content_type="image/png", tenant_id=OWNER["tenant_id"],
                        user_id=OWNER["user_id"], ttl_s=0)
        assert store.reap() == 1
        assert store.state(ref.artifact_id, tenant_id=OWNER["tenant_id"],
                           user_id=OWNER["user_id"]) is art.ArtifactState.EXPIRED

    def test_another_identity_cannot_read_it(self, store):
        ref = store.put(data=b"png", kind="browser_screenshot", content_type="image/png",
                        tenant_id="tenant-a", user_id="alice")
        assert store.state(ref.artifact_id, tenant_id="tenant-b",
                           user_id="alice") is art.ArtifactState.DENIED
        assert store.state(ref.artifact_id, tenant_id="tenant-a",
                           user_id="bob") is art.ArtifactState.DENIED
        with pytest.raises(art.ArtifactError):
            store.get(ref.artifact_id, tenant_id="tenant-b", user_id="mallory")

    def test_an_unowned_artifact_cannot_be_stored(self, store):
        with pytest.raises(art.ArtifactError):
            store.put(data=b"x", kind="k", content_type="image/png",
                      tenant_id="", user_id="u")

    def test_a_failed_persist_is_reported_not_papered_over(self, gw, store, monkeypatch):
        """A reference that looks durable and is not is worse than no screenshot:
        nobody checks it until the approval."""
        def _boom(**kw):
            raise art.ArtifactError(art.ArtifactState.MISSING, "disk full")
        monkeypatch.setattr(store, "put", _boom)
        gw.observation = {"ok": True, "action": "browser_screenshot",
                          "url": "http://lab/x", "screenshot_ref": "shot_v",
                          "extracted": {"masked_fields": 0}}
        res = _await(TOOLS["browser_screenshot"](
            **OWNER, browser_session_id="bs_x", gateway=gw, store=store))
        assert res.outcome is Outcome.FAILED
        assert res.artifact_ref is None

    def test_a_missing_worker_ref_is_reported(self, gw, store):
        gw.observation = {"ok": True, "action": "browser_screenshot",
                          "url": "http://lab/x"}      # no screenshot_ref
        res = _await(TOOLS["browser_screenshot"](
            **OWNER, browser_session_id="bs_x", gateway=gw, store=store))
        assert res.outcome is Outcome.FAILED
        assert res.artifact_ref is None


# ══════════════════════════════════════════════════════════════════════════════
# 10. Boundedness (§7.3)
# ══════════════════════════════════════════════════════════════════════════════
class TestBoundedness:
    def test_truncation_is_stated_in_words(self, gw):
        gw.observation = {
            "ok": True, "action": "browser_inspect", "element_total": 214,
            "element_truncated": True,
            "elements": [{"ref": f"e{i}", "role": "button", "accessible_name": f"b{i}",
                          "type": "button", "state": {}} for i in range(1, 61)],
        }
        res = _await(TOOLS["browser_inspect"](
            **OWNER, browser_session_id="bs_x", gateway=gw))
        assert "60 of 214" in res.summary
        assert "60 of 214" in res.for_model()
        assert "154 more not shown" in res.for_model()


# ══════════════════════════════════════════════════════════════════════════════
# 11. End to end against the real worker and lab
# ══════════════════════════════════════════════════════════════════════════════
@pytest_asyncio.fixture
async def live(require_chromium, lab_url):
    from playwright_worker import BrowserWorker

    from tests.browser_driver import reset_lab
    reset_lab(lab_url)
    w = BrowserWorker()
    await w.start()
    try:
        yield bt.in_process_gateway(w), lab_url
    finally:
        await w.stop()


@pytest.mark.browser
@pytest.mark.lab
@pytest.mark.asyncio
class TestAgainstTheRealWorker:
    async def _open(self, gateway, lab_url):
        res = await TOOLS["browser_open"](**OWNER, gateway=gateway,
                                          allowed_domains=["127.0.0.1", "localhost"])
        assert res.ok, res.error_message
        return res.browser_session_id

    async def _ref(self, gateway, bs, name):
        r = await TOOLS["browser_inspect"](**OWNER, browser_session_id=bs, gateway=gateway)
        for e in r.elements:
            if name.lower() in e.name.lower():
                return e.ref
        return None

    async def test_click_on_the_real_submit_button_points_at_browser_submit(self, live):
        gateway, lab_url = live
        bs = await self._open(gateway, lab_url)
        await TOOLS["browser_navigate"](**OWNER, browser_session_id=bs,
                                        url=f"{lab_url}/login", gateway=gateway)
        ref = await self._ref(gateway, bs, "Continue")
        res = await TOOLS["browser_click"](**OWNER, browser_session_id=bs,
                                           element_ref=ref, gateway=gateway)
        assert res.outcome is Outcome.FAILED
        assert res.detail["use_instead"] == "browser_submit"
        assert "browser_submit" in res.recovery
        assert res.terminal is False

    async def test_a_real_stale_ref_surfaces_stale_ref(self, live):
        gateway, lab_url = live
        bs = await self._open(gateway, lab_url)
        await TOOLS["browser_navigate"](**OWNER, browser_session_id=bs,
                                        url=f"{lab_url}/login", gateway=gateway)
        ref = await self._ref(gateway, bs, "Email address")
        await TOOLS["browser_navigate"](**OWNER, browser_session_id=bs,
                                        url=f"{lab_url}/careers", gateway=gateway)
        res = await TOOLS["browser_fill"](**OWNER, browser_session_id=bs,
                                          element_ref=ref, value="x", gateway=gateway)
        assert res.error_code == "STALE_REF"
        assert "re-inspect" in res.recovery.lower()
        assert res.terminal is False

    async def test_the_lab_server_rejection_does_not_read_as_done(self, live, tmp_path):
        """Required: a fill the lab's server_reject rejects must not produce a result
        an agent would read as done."""
        from tests.browser_driver import set_flags
        gateway, lab_url = live
        set_flags(lab_url, server_reject=True)
        bs = await self._open(gateway, lab_url)

        async def fill(name, value, **kw):
            ref = await self._ref(gateway, bs, name)
            assert ref, name
            return await TOOLS["browser_fill"](**OWNER, browser_session_id=bs,
                                                element_ref=ref, value=value,
                                                gateway=gateway, **kw)

        await TOOLS["browser_navigate"](**OWNER, browser_session_id=bs,
                                        url=f"{lab_url}/login", gateway=gateway)
        await fill("Email address", FIXTURE_EMAIL)
        await fill("Pass phrase", FIXTURE_PASSWORD, sensitive=True)
        ref = await self._ref(gateway, bs, "Continue")
        await TOOLS["browser_submit"](**OWNER, browser_session_id=bs,
                                       element_ref=ref, gateway=gateway)

        ref = await self._ref(gateway, bs, "Name")
        await TOOLS["browser_fill"](**OWNER, browser_session_id=bs, element_ref=ref,
                                     value="Demo Person", gateway=gateway)
        ref = await self._ref(gateway, bs, "Save and continue")
        await TOOLS["browser_submit"](**OWNER, browser_session_id=bs,
                                       element_ref=ref, gateway=gateway)

        await fill("Full name", "Ada Lovelace")
        await fill("Email address", "ada@browser-lab.invalid")
        await fill("Telephone", "+971 4 555 0100")
        dept = await self._ref(gateway, bs, "Department")
        await TOOLS["browser_select"](**OWNER, browser_session_id=bs,
                                       element_ref=dept, value="ops", gateway=gateway)
        await fill("Earliest start date", "2026-09-01")
        pref = await self._ref(gateway, bs, "Email")
        await TOOLS["browser_check"](**OWNER, browser_session_id=bs,
                                      element_ref=pref, checked=True, gateway=gateway)

        submit = await self._ref(gateway, bs, "Submit application")
        res = await TOOLS["browser_submit"](**OWNER, browser_session_id=bs,
                                             element_ref=submit, gateway=gateway)

        assert res.ok is False, "a server-rejected submit read as success"
        assert res.outcome is Outcome.NEEDS_CORRECTION
        assert res.field_errors, "the page's complaint was not surfaced"
        text = res.for_model()
        assert text.startswith("NEEDS CORRECTION")
        assert "correct" in res.recovery.lower()

    async def test_a_real_screenshot_is_durable_and_masked(self, live, tmp_path):
        gateway, lab_url = live
        store = art.FilesystemArtifactStore(tmp_path / "arts")
        bs = await self._open(gateway, lab_url)
        await TOOLS["browser_navigate"](**OWNER, browser_session_id=bs,
                                        url=f"{lab_url}/login", gateway=gateway)
        await TOOLS["browser_inspect"](**OWNER, browser_session_id=bs, gateway=gateway)
        res = await TOOLS["browser_screenshot"](**OWNER, browser_session_id=bs,
                                                 gateway=gateway, store=store)
        assert res.ok, res.error_message
        assert res.artifact_ref.startswith("art_")
        assert res.detail["masked_fields"] >= 1, "no mask was applied"

        reopened = art.FilesystemArtifactStore(tmp_path / "arts")
        data, state = bt.fetch_screenshot(res.artifact_ref, tenant_id=OWNER["tenant_id"],
                                          user_id=OWNER["user_id"], store=reopened)
        assert state is art.ArtifactState.AVAILABLE
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert data[:8] not in json.dumps(res.as_dict()).encode()
