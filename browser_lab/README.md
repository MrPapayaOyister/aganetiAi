# Browser Lab

The hermetic dummy site from `docs/automation-architecture.md` §10. A **test
target** for browser automation — not a product service, and not wired into the
orchestrator, the tool registry, or any agent. Nothing here imports from
`backend/`, and nothing in `backend/` should import from here.

There is no Playwright in this package. The lab is what gets driven; the driver
lives in the (not-yet-built) `playwright-worker`.

## Running it

```bash
docker compose up -d browser-lab          # http://localhost:8080
python -m browser_lab                     # or straight from the repo venv
```

Sibling compose services reach it as **`http://browser-lab:8080`** — the compose
service DNS name. Not `browser-lab.local`: that is mDNS and will not resolve
reliably from inside a worker container (§10.1).

## The flow

`/login` → `/profile` → `/application` → `/confirmation`

`/careers` sits deliberately outside that path, linked from every page, so
navigation limits have somewhere to be exceeded (§10.3).

`/application` carries the full element inventory: text inputs, email, phone,
a select, a radio group, checkboxes, a date picker, a file upload, and submit.

## Credentials

Fake, committed, and obviously fake (§10.4). `.invalid` is reserved by RFC 2606
and can never resolve.

| Reference | Email |
|---|---|
| `lab.applicant` | `demo@browser-lab.invalid` |
| `lab.reviewer` | `reviewer@browser-lab.invalid` |

They live in `fixtures.py` and are served by reference from
`GET /_test/credentials` — the v1 stand-in for the credential broker. The agent
gets the *reference*; something else resolves it.

## Failure modes

All off after a reset. Each is individually triggerable, two ways:

```bash
GET /application?fail=server_reject            # per-request
POST /_test/flags {"server_reject": true}      # sticky until reset
```

They compose (the effective set is the union), and `?fail=none` forces the
per-request set empty regardless of what is sticky. Forms carry the active set
through the POST in a hidden `_fail` input, so a mode enabled on the rendered
page still applies to the submission.

| Mode | What it does |
|---|---|
| `dependent_field` | Employer block hidden until phone is non-empty; server enforces the same rule |
| `delayed_element` | Consent panel absent from the initial HTML; `/application/consent-panel` returns **425** until the delay elapses |
| `disabled_submit` | Submit renders `disabled`, enabled by script once required fields are filled |
| `server_reject` | First submit of a session rejected with a readable message about a *plausible-looking* phone number |
| `confirm_dialog` | Submitting renders a confirmation step; the real submit needs the token it echoes |
| `unprompted_modal` | Unrequested interstitial on `/profile` only |

`GET /_test/failure-modes` returns the catalog.

## Control surface

Everything is namespaced under `/_test` so it cannot be mistaken for part of the
site under test.

| Endpoint | Purpose |
|---|---|
| `POST /_test/reset` | Return to a known state — sessions, submissions, flags, id counters |
| `GET /_test/submissions` | **What the server actually received**, rejected attempts included |
| `GET`/`POST /_test/flags` | Read/sticky-set failure modes; `delay_ms` retunes the delayed element |
| `GET /_test/failure-modes` | The catalog |
| `GET /_test/credentials` | Fake credentials, by reference |
| `GET /_test/health` | Liveness |

`/_test/submissions` is the point of the lab. Asserting that a confirmation page
rendered says nothing about the payload, and §8.2(b)'s approval-revalidation
test needs the payload itself.

Ids are deterministic across a reset: the first session is always `sess-1`, the
first submission always `sub-1`.

## Addressability is uneven on purpose

Most interactive elements carry a `data-testid`. A deliberate subset does not
(§10.3) — a lab where everything is trivially addressable proves nothing about
the DOM strategy:

| Element | Test id | How it must be found |
|---|---|---|
| login password | **no** | `for=` only; label reads "Pass phrase" |
| profile display name | **no** | label *also* reads "Name" — duplicated; `aria-labelledby` |
| application 2nd reference | **no** | label *also* "Reference"; `aria-labelledby` |
| application phone | **no** | `aria-labelledby` only — nothing uses `for="phone"` |
| contact-preference radios | **no** | fieldset legend + per-option `for=` |
| notification checkbox #3 | **no** | `for=` only |

## Hermetic

No CDN fonts, no external stylesheets, no remote images, no telemetry, no
`fetch` to anything but this origin. The only font reference is the system
stack. `/docs` and `/redoc` are disabled — Swagger pulls its bundle from a CDN.

Verified, not assumed:

```bash
docker run -d --network none --name lab_hermetic aganetiai-browser-lab
docker exec lab_hermetic python -c "import urllib.request; \
  print(urllib.request.urlopen('http://127.0.0.1:8080/_test/health').read())"
```

`tests/test_browser_lab.py::test_no_page_references_an_external_origin` scans
every page for external references, so a pasted CDN link fails CI rather than
only failing on an air-gapped runner.

## Tests

```bash
pytest tests/test_browser_lab.py                                    # in-process
BROWSER_LAB_URL=http://localhost:8080 pytest tests/test_browser_lab.py   # live container
```

Same suite either way. The first form runs in CI without a daemon; the second
proves the Docker service works. Every test resets first, so nothing depends on
execution order.
