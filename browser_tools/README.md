# browser_tools

The semantic tool layer from `docs/automation-architecture.md` §3 — Phase C.

```
tool call -> validate -> BrowserCommand -> worker -> BrowserObservation -> ToolResult
```

**Registers nothing.** No registry entry, no `guardrails.TOOL_CATEGORY` edit, no
authorization — Phases D and E. **Imports nothing from `backend/`**, so it does not
know `TenantContext` exists: ownership arrives as four explicit strings.

## Layout

| File | Role |
|---|---|
| `schemas.py` | the 14 model-facing schemas; import-time guards |
| `outcomes.py` | worker code → outcome, §9.1 recovery, terminality |
| `results.py` | `ToolResult`, the §7.2 scrub, the model rendering |
| `tools.py` | the 14 tool functions: validate → gateway → result |
| `client.py` | **the one door to the worker**, with Phase E's hook |
| `artifacts.py` | durable screenshot store; MISSING ≠ EXPIRED |

## Four properties, each with a test that walks every tool

**1. No selector channel.** No schema may declare `selector`, `css`, `xpath`,
`script`, `code`, `js`, `evaluate`, `path` or 20 other names. Checked at import
(`_verify_no_forbidden_params`) *and* in CI. Selector-shaped `element_ref` values are
refused by this layer **and, independently, by the worker** — two layers with their
own reasons, because one layer is one refactor from none.

**2. Ownership is explicit.** `tenant_id`, `user_id`, `agent_id`, `session_id` are
required on all 14. Phase D fills them; nothing here reads ambient identity.

**3. A validation rejection is not success.** The worker returns `VALIDATION_ERROR`
with `ok=True` — correct at the transport layer, wrong at this one. `ToolResult` is
three-valued:

| Outcome | Meaning |
|---|---|
| `OK` | the action did what was asked |
| `NEEDS_CORRECTION` | the page rejected the input — named field, message, recovery |
| `FAILED` | the action did not happen |

`result.ok` is `True` **only** for `OK`. A rejected form cannot satisfy `if
result.ok`, and `for_model()` opens with `NEEDS CORRECTION`.

**4. §7.2 holds.** No field on `ToolResult` can carry cookies, `storageState`,
tokens, raw DOM or image bytes; `_scrub` is a second pass over `detail`. A sensitive
fill records *that* a value was set, never what — including the lab's fake password.

## Errors

The 10 §9.1 codes keep their documented recovery. The 4 worker-boundary codes —
`BAD_REQUEST`, `SELECTOR_REJECTED`, `SESSION_NOT_FOUND`, `BUDGET_EXCEEDED` — are all
terminal and none carries a retry. `assert_taxonomy_complete()` fails if a worker
code is unclassified, double-classified, or has no recovery.

One deliberate softening: a `browser_click` refused for being submit-like comes back
`AUTHZ_DENIED`, whose generic recovery is "stop". Here it is rewritten to point at
`browser_submit` and marked non-terminal — an agent that stops has abandoned a task
it could finish. A genuine `AUTHZ_DENIED` stays terminal.

## Screenshots are durable

`browser_screenshot` does **capture → fetch → persist → return a durable id**. The
worker's own ref is process-memory and dies with the worker; §8.2(c)'s approval
payload is reviewed minutes to hours later, so a volatile ref would be a broken
image exactly when a human is deciding whether to submit a form.

Bytes exist in this process only between fetch and put. They never reach a
`ToolResult`, which carries `artifact_ref` and provenance only.

```
<root>/<artifact_id>/manifest.json   ← outlives the bytes, deliberately
<root>/<artifact_id>/blob            ← deleted on expiry
```

That is the MISSING-vs-EXPIRED mechanism: no manifest = never existed; manifest
without blob = aged out. A reviewer needs different responses to those.

### On the store itself

A production object store **already exists** — `backend/storage/object_store.py`
(`SeaweedFSStorage`, S3 SigV4, four live containers) plus a `chat_artifacts` table
with `uri`/`byte_size`/`meta`. This package may not import it. So `ArtifactStore` is
a Protocol shaped like the existing `StorageService`, with a filesystem default;
Phase D calls `set_artifact_store()` once with a SeaweedFS-backed adapter and no
tool changes. The default is durable in the sense required — it survives a restart —
and is not a production store: local disk, no replication, no quota.

## One door to the worker

Every tool reaches the worker through `WorkerGateway.call()`. `_authorize()` on that
path is an empty hook that exists now so Phase E is a body rather than an
architecture change, and so its anti-bypass test has one function to assert against.
A test greps `tools.py` for any other route.

Two transports, identical interface: `InProcessTransport` (tests) and
`HttpTransport` (deployed).

## Tests

```bash
pytest tests/test_browser_tools.py                    # 186
pytest tests/test_browser_tools.py -m "not browser"   # 182, no Chromium needed
```

Markers and probes come from `tests/conftest.py`. Four tests drive the real worker
and lab; the rest fake the gateway, so the mapping is provable anywhere.
