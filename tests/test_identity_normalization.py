"""The auth middleware must impose the caller's identity, not merely correct it.

The bug this pins shut: normalization was conditional —

    if qs and "user_id=" in qs:      # only rewrite what the client already sent
        params["user_id"] = [effective_user]

Dozens of handlers are declared `user_id: str = "user_1"`. A request that sent a
user_id got it corrected; a request that OMITTED it skipped the rewrite entirely
and fell through to that default, running against the seeded admin's data. The
protection was effectively opt-in from the client's side, and the safe-looking
call — just don't pass a user_id — was the one that leaked.

Same shape for the JSON body.
"""

import json

import pytest

from backend.auth import enforce


class _Recorder:
    """Stands in for the ASGI app and captures the scope it is handed."""

    def __init__(self):
        self.scope = None
        self.body = None

    async def __call__(self, scope, receive, send):
        self.scope = scope
        msg = await receive()
        self.body = msg.get("body", b"")


def _scope(method="GET", path="/tasks", query=b"", headers=None):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": headers or [],
        "client": ("10.0.0.5", 1234),
    }


async def _noop_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _noop_send(msg):
    return None


def _query(scope) -> dict:
    from urllib.parse import parse_qs
    return parse_qs(scope["query_string"].decode())


@pytest.mark.asyncio
async def test_user_id_is_injected_when_the_client_omits_it():
    """THE regression. An absent user_id must not reach a handler default."""
    app = _Recorder()
    mw = enforce.AuthEnforceMiddleware(app)
    await mw._forward(_scope(query=b"limit=10"), _noop_receive, _noop_send, "sub-abc", "")
    assert _query(app.scope)["user_id"] == ["sub-abc"]
    assert _query(app.scope)["limit"] == ["10"], "other params must survive"


@pytest.mark.asyncio
async def test_user_id_is_injected_on_a_completely_empty_query():
    app = _Recorder()
    mw = enforce.AuthEnforceMiddleware(app)
    await mw._forward(_scope(query=b""), _noop_receive, _noop_send, "sub-abc", "")
    assert _query(app.scope)["user_id"] == ["sub-abc"]


@pytest.mark.asyncio
async def test_a_forged_user_id_is_overwritten_not_appended():
    """The IDOR case: claiming someone else's id must not survive, and must not
    produce two values where a handler might read the attacker's."""
    app = _Recorder()
    mw = enforce.AuthEnforceMiddleware(app)
    await mw._forward(_scope(query=b"user_id=victim-sub"), _noop_receive, _noop_send,
                      "sub-abc", "")
    assert _query(app.scope)["user_id"] == ["sub-abc"]


@pytest.mark.asyncio
async def test_json_body_user_id_is_set_even_when_absent():
    app = _Recorder()
    mw = enforce.AuthEnforceMiddleware(app)
    body = json.dumps({"message": "hi"}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = _scope(method="POST", path="/chat",
                   headers=[(b"content-type", b"application/json")])
    await mw._forward(scope, receive, _noop_send, "sub-abc", "")
    assert json.loads(app.body)["user_id"] == "sub-abc"
    assert json.loads(app.body)["message"] == "hi", "the rest of the body must survive"


@pytest.mark.asyncio
async def test_json_body_user_id_is_overwritten():
    app = _Recorder()
    mw = enforce.AuthEnforceMiddleware(app)
    body = json.dumps({"user_id": "victim-sub", "message": "hi"}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = _scope(method="POST", path="/chat",
                   headers=[(b"content-type", b"application/json")])
    await mw._forward(scope, receive, _noop_send, "sub-abc", "")
    assert json.loads(app.body)["user_id"] == "sub-abc"


@pytest.mark.asyncio
async def test_client_supplied_auth_header_is_stripped():
    """x-auth-user is what handlers trust, so a caller must not be able to set it."""
    app = _Recorder()
    mw = enforce.AuthEnforceMiddleware(app)
    scope = _scope(headers=[(b"x-auth-user", b"victim-sub")])
    await mw._forward(scope, _noop_receive, _noop_send, "sub-abc", "")
    injected = [v for k, v in app.scope["headers"] if k == b"x-auth-user"]
    assert injected == [b"sub-abc"], injected


@pytest.mark.asyncio
async def test_content_length_matches_the_rewritten_body():
    """A stale Content-Length either truncates the body or hangs the read."""
    app = _Recorder()
    mw = enforce.AuthEnforceMiddleware(app)
    body = json.dumps({"message": "hi"}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = _scope(method="POST", path="/chat",
                   headers=[(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())])
    await mw._forward(scope, receive, _noop_send, "sub-abc", "")
    declared = int(dict(app.scope["headers"])[b"content-length"])
    assert declared == len(app.body)


def test_no_hardcoded_user_fallback_remains_in_the_middleware():
    """Structural. Every identity in this file must come from the caller's token;
    a literal id here is a way to act as somebody without authenticating as them.

    Asserted against the parsed AST with docstrings removed, not the raw source —
    this module's own prose describes the bug it fixed, and matching on text would
    fail on the explanation rather than on any executable code.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(enforce))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]

    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert "user_1" not in literals, \
        "a hardcoded user id is still reachable in the auth path"


def test_no_request_handler_defaults_to_a_real_user():
    """No endpoint may fall back to a named account when identity is absent.

    Dozens were declared `user_id: str = "user_1"`. The middleware now injects the
    caller's id unconditionally, which makes those defaults unreachable — but an
    unreachable default that names a real person is one refactor away from being
    reachable again, and it fails OPEN when it is.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in [root / "backend" / "main.py", root / "backend" / "service_auth.py"]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            for arg, default in zip(args.args[-len(args.defaults):] if args.defaults else [],
                                    args.defaults):
                if arg.arg != "user_id":
                    continue
                if isinstance(default, ast.Constant) and default.value in ("user_1", "user_2"):
                    offenders.append(f"{path.name}:{node.lineno} {node.name}")
    assert not offenders, "handlers defaulting to a named account: " + ", ".join(offenders)


def test_internal_headers_will_not_invent_a_caller():
    """An internal call must name whom it acts for. The default used to be the
    seeded admin, so a caller that forgot ran against that person's data."""
    import inspect

    from backend import service_auth

    sig = inspect.signature(service_auth.internal_headers)
    assert sig.parameters["user_id"].default is inspect.Parameter.empty, \
        "internal_headers must require an explicit user"
    with pytest.raises(ValueError):
        service_auth.internal_headers("")
