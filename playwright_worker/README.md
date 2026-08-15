# playwright-worker

The browser execution backend from `docs/automation-architecture.md` §6. Drives
Chromium behind **one** operation:

```
execute(BrowserCommand) -> BrowserObservation
```

Nothing here imports from `backend/`, and nothing in `backend/` imports from here.
There are **no tool schemas** in this package — registry integration is Phase C.

## Running it

```bash
docker compose up -d browser-lab playwright-worker   # worker on host :8091
python -m playwright_worker                          # or from the repo venv, :8090
```

Host port is **8091**, container port **8090**. This machine already publishes 8090
for `seaweed-volume`, which is not part of this compose file. Sibling services reach
the worker at `http://playwright-worker:8090` and the lab at
`http://browser-lab:8080`.

## Surface

| Endpoint | Purpose |
|---|---|
| `POST /execute` | the one operation; always 200 with a typed observation |
| `GET /screenshot/{ref}` | stored bytes, by reference, ownership-checked |
| `GET /health` | liveness + live session count + effective budgets |

There is deliberately no `/click`, `/navigate` or per-action route: a route per
action would drift from the closed enum the moment someone added one without
updating the other.

## The 14 actions

`browser_open` `browser_close` `browser_navigate` `browser_inspect`
`browser_screenshot` `browser_extract` `browser_wait` `browser_back`
`browser_click` `browser_fill` `browser_select` `browser_check` `browser_upload`
`browser_submit`

The set is closed (`protocol.Action`). An unknown action is `BAD_REQUEST` before a
page is touched.

## What the worker will not accept

- **No code, scripts or selectors.** `FORBIDDEN_ARGS` names `selector`, `css`,
  `xpath`, `script`, `code`, `js`, `evaluate`, `path` and more; every one is refused
  on every action with `SELECTOR_REJECTED`.
- **`element_ref` only.** Refs look like `e17` and are issued by `browser_inspect`.
  Anything selector-shaped — `#id`, `.cls`, `//xpath`, `css=`, `text=` — is refused
  at the boundary, tested with 19 hostile inputs.
- **No `page.evaluate` tool.** The single fixed collection script lives in
  `inspector.py`, takes only a worker-generated nonce, and no command argument
  reaches it.
- **No filesystem paths.** Uploads take an `artifact_id` resolved worker-side.

## Ref map and staleness

`browser_inspect` stamps a per-generation nonce (`data-pw-ref`) on each interactive
element and returns at most 60 as `{ref, role, accessible_name, label, type, state}`.
The ref→locator map is worker-side and session-scoped.

Refs are invalidated on navigation, on `browser_back`, and after any click. A ref
from an older generation returns `STALE_REF` — never a wrong-element action. That is
the whole reason for the indirection, and it is why refs are bound to a nonce rather
than to a CSS path: a path silently matches a *different* element after a re-render.

Truncation is visible (`element_total`, `element_truncated`), per §7.3.

## Submit-like refusal (§3.4)

The inspector classifies `<button type=submit>`, `<input type=submit|image>`, and a
bare `<button>` inside a form — the HTML default, and the case a naive check misses.

`browser_click` **refuses** those with `AUTHZ_DENIED` pointing at `browser_submit`.
`browser_submit` refuses anything that is *not* submit-like, so the two are a real
partition. The classification is read from the inspector's walk, and there is no
path to a click that skips inspection, so it cannot be bypassed by declining to
inspect.

## Errors (§9.1)

`ELEMENT_NOT_FOUND` `STALE_REF` `ELEMENT_NOT_VISIBLE` `ELEMENT_DISABLED` `TIMEOUT`
`NAVIGATION_FAILED` `VALIDATION_ERROR` `UNEXPECTED_MODAL` `DOMAIN_DENIED`
`AUTHZ_DENIED`, plus four worker-boundary codes (`BAD_REQUEST`,
`SELECTOR_REJECTED`, `SESSION_NOT_FOUND`, `BUDGET_EXCEEDED`).

Every observation carries `recovery` (the documented §9.1 policy) and `terminal`.
`VALIDATION_ERROR` is returned with **`ok=True`** — §9.1 calls it an expected
signal, and marking it a failure would make every caller treat a form that needs
correcting as something that went wrong.

## Budgets (§9.2) — enforced worker-side

| Budget | Default | Env |
|---|---|---|
| `max_actions_per_task` | 40 | `BROWSER_MAX_ACTIONS_PER_TASK` |
| `max_retries_per_element` | 2 | `BROWSER_MAX_RETRIES_PER_ELEMENT` |
| `max_navigations_per_task` | 10 | `BROWSER_MAX_NAVIGATIONS_PER_TASK` |
| `action_timeout_ms` | 15000 | `BROWSER_ACTION_TIMEOUT_MS` |
| `task_wall_clock_ms` | 300000 | `BROWSER_TASK_WALL_CLOCK_MS` |
| `max_inspect_elements` | 60 | `BROWSER_MAX_INSPECT_ELEMENTS` |

Counters live on the session, so a caller that forgets to count — or lies —
changes nothing. Actions are charged **before** the work: charging after would let a
budget of N permit N+1, and for `browser_submit` the extra one is irreversible.
Exhaustion is a clean terminal observation with partial progress, never a crash.

## Sessions (§5)

One `BrowserContext` per session, one shared `Browser`. Ids are
`bs_` + 256 bits. Downloads disabled, permissions denied, no `storageState` reuse,
no global context. Actions within a session are serialized.

Ownership is asserted on every call. A mismatch returns `SESSION_NOT_FOUND` — the
same answer as a session that never existed, because a distinguishable denial
confirms the session exists and leaks other tenants' work.

| Limit | Default | Env |
|---|---|---|
| idle TTL | 900 s | `BROWSER_IDLE_TTL_S` |
| absolute TTL | 7200 s | `BROWSER_ABSOLUTE_TTL_S` |
| sessions per user | 3 | `BROWSER_MAX_SESSIONS_PER_USER` |
| sessions per tenant | 10 | `BROWSER_MAX_SESSIONS_PER_TENANT` |

Reaping is a supervised loop that survives an exception in any single close and
removes a session from the registry *before* awaiting its close, so a slow close
cannot be handed out again.

## Screenshots

Masked **before** encoding via Playwright's `mask=`, so unmasked bytes never exist.
Password inputs are masked as a floor even on a page that was never inspected.
Stored by reference with a TTL; the observation carries `screenshot_ref` and never
the bytes. Closing a session drops its shots.

## Tests

```bash
pytest tests/test_browser_worker.py                  # 116, needs Chromium + lab
pytest tests/test_browser_worker.py -m "not browser" # 67, pure Python
```

Markers `browser`, `lab` and `slow` are registered in `tests/conftest.py`, which
also holds the availability probes. Every skip decision is made at **run** time — a
collection-time probe builds process-wide singletons before other suites set up
theirs, which is how a probe in `test_graph_search_tool.py` once broke four
unrelated tests.
