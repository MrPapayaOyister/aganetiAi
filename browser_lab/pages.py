"""Browser Lab HTML.

Hand-rolled strings rather than Jinja2 (which the repo does use, in
`backend/documents.py`) so the lab image needs nothing beyond FastAPI and
uvicorn. Fewer dependencies is fewer ways for a "hermetic" fixture to turn out
not to be.

**Hermetic (§10.1).** No CDN fonts, no external stylesheets, no remote images,
no analytics, no `fetch` to anything but this origin. The only font reference is
the system stack, which resolves locally by definition. If a page here ever
needs an asset it does not carry, that is a bug and not a convenience.

**Mixed addressability (§10.3).** A lab where every element answers to a
`data-testid` proves nothing about the DOM strategy, because real sites do not
look like that. So the awkwardness is deliberate and distributed:

| Element                        | Test id | How a human/agent must find it        |
|--------------------------------|---------|---------------------------------------|
| login email                    | yes     | `data-testid`, or `for=`              |
| login password                 | **no**  | `for=` only; label reads "Pass phrase"|
| profile full name              | yes     | label "Name"                          |
| profile display name           | **no**  | label *also* reads "Name" — duplicated|
| application reference (first)  | yes     | label "Reference"                     |
| application reference (second) | **no**  | label *also* "Reference"; aria-labelledby |
| application phone              | **no**  | `aria-labelledby` only — no `for=`    |
| contact-preference radios      | **no**  | fieldset legend + per-option `for=`   |
| notification checkbox #3       | **no**  | `for=` only                           |

Everything else carries a `data-testid`, per §10.3's "stable test id on every
interactive element" — the point is that the agent must not be *able* to rely
on them exclusively, not that they are absent.
"""
from __future__ import annotations

from html import escape as esc

from browser_lab import fixtures

# System stack only. Naming a webfont here would need a CDN, and a lab that
# needs egress is not a lab.
_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  margin: 0; padding: 2rem 1rem; line-height: 1.5;
  background: #f6f7f9; color: #14171a;
}
main { max-width: 46rem; margin: 0 auto; background: #fff;
       border: 1px solid #d7dbe0; border-radius: 8px; padding: 1.5rem 1.75rem; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
p.sub { color: #5a6570; margin: 0 0 1.5rem; font-size: .9rem; }
nav { max-width: 46rem; margin: 0 auto 1rem; font-size: .85rem; color: #5a6570; }
nav a { color: #2d5bd7; margin-right: .9rem; }
fieldset { border: 1px solid #d7dbe0; border-radius: 6px; margin: 0 0 1.25rem; padding: .9rem 1rem; }
legend { font-weight: 600; font-size: .9rem; padding: 0 .35rem; }
label { display: block; font-weight: 500; margin: 0 0 .3rem; font-size: .9rem; }
.field { margin: 0 0 1.1rem; }
input[type=text], input[type=email], input[type=tel], input[type=date],
input[type=password], select {
  width: 100%; padding: .5rem .6rem; font: inherit;
  border: 1px solid #b9c0c8; border-radius: 5px; background: #fff;
}
.opt { display: flex; align-items: center; gap: .5rem; margin: .3rem 0; font-weight: 400; }
.opt label { display: inline; font-weight: 400; margin: 0; }
button { font: inherit; font-weight: 600; padding: .55rem 1.15rem; border-radius: 5px;
         border: 1px solid #2d5bd7; background: #2d5bd7; color: #fff; cursor: pointer; }
button[disabled] { background: #aeb6c0; border-color: #aeb6c0; cursor: not-allowed; }
button.secondary { background: #fff; color: #2d5bd7; }
.errors { border: 1px solid #c8372d; background: #fdf2f1; color: #8c231b;
          border-radius: 6px; padding: .75rem 1rem; margin: 0 0 1.25rem; }
.errors ul { margin: .4rem 0 0; padding-left: 1.1rem; }
.notice { border: 1px solid #c9a227; background: #fffbe9; border-radius: 6px;
          padding: .75rem 1rem; margin: 0 0 1.25rem; font-size: .9rem; }
.modal-backdrop { position: fixed; inset: 0; background: rgba(12,16,22,.55);
                  display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { background: #fff; border-radius: 8px; padding: 1.5rem; max-width: 27rem; }
.modal h2 { margin: 0 0 .5rem; font-size: 1.1rem; }
dl.summary { display: grid; grid-template-columns: max-content 1fr; gap: .3rem .9rem;
             margin: 0; font-size: .9rem; }
dl.summary dt { color: #5a6570; }
dl.summary dd { margin: 0; font-weight: 500; word-break: break-word; }
.hint { font-size: .8rem; color: #5a6570; margin: .25rem 0 0; font-weight: 400; }
"""


def _testid(value: str | None) -> str:
    """Render a `data-testid`, or nothing at all for the deliberately-bare set."""
    return f' data-testid="{esc(value)}"' if value else ""


def layout(title: str, body: str, *, extra_head: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Browser Lab</title>
<style>{_CSS}</style>
{extra_head}
</head>
<body>
<nav data-testid="lab-nav">
  <a href="/login" data-testid="nav-login">Sign in</a>
  <a href="/profile" data-testid="nav-profile">Profile</a>
  <a href="/application" data-testid="nav-application">Application</a>
  <!-- Off the happy path on purpose (§10.3). Linked from every page so a
       navigation-limit test has something real to wander into. -->
  <a href="/careers" data-testid="nav-careers">Open roles</a>
</nav>
<main>
{body}
</main>
</body>
</html>"""


def _errors_block(errors: dict[str, str]) -> str:
    if not errors:
        return ""
    items = "".join(
        f'<li data-testid="error-{esc(f)}"><strong>{esc(f)}</strong>: {esc(m)}</li>'
        for f, m in errors.items()
    )
    return (
        '<div class="errors" role="alert" data-testid="form-errors">'
        "<strong>We could not accept this form.</strong>"
        f"<ul>{items}</ul></div>"
    )


def _hidden_flags(active: list[str]) -> str:
    """Carry the per-request failure modes through the POST.

    Without this a form rendered with `?fail=server_reject` would post to a
    clean `/application` and the mode would silently not apply — the failure
    would look like the lab not working rather than the flag not sticking.
    """
    return f'<input type="hidden" name="_fail" value="{esc(",".join(active))}">'


# ── /login ────────────────────────────────────────────────────────────────────
def login_page(*, errors: dict[str, str], qs: str, active: list[str]) -> str:
    body = f"""
<h1>Sign in</h1>
<p class="sub">Browser Lab — a fixture. Credentials are fake and committed.</p>
{_errors_block(errors)}
<form method="post" action="/login{qs}" data-testid="login-form">
  {_hidden_flags(active)}
  <div class="field">
    <label for="email">Email address</label>
    <input type="email" id="email" name="email" autocomplete="off"
           placeholder="you@example.invalid" data-testid="login-email">
  </div>
  <div class="field">
    <!-- No test id, and the visible label is not the word anyone would search
         for. Association resolves only through for=/id. -->
    <label for="pw-field">Pass phrase</label>
    <input type="password" id="pw-field" name="password" autocomplete="off">
  </div>
  <button type="submit" data-testid="login-submit">Continue</button>
</form>
"""
    return layout("Sign in", body)


# ── /profile ──────────────────────────────────────────────────────────────────
def profile_page(
    *, session_email: str, errors: dict[str, str], values: dict[str, str],
    qs: str, active: list[str], show_modal: bool,
) -> str:
    options = "".join(
        f'<option value="{esc(v)}"{" selected" if values.get("timezone") == v else ""}>{esc(t)}</option>'
        for v, t in fixtures.TIMEZONES
    )

    # The unprompted modal (§10.3). Rendered server-side and on /profile only,
    # so "unexpected modal" is a property of one path rather than of the whole
    # lab — that is what makes it a *specific* thing to detect and dismiss.
    modal = ""
    if show_modal:
        modal = """
<div class="modal-backdrop" data-testid="interstitial-modal" role="dialog"
     aria-modal="true" aria-labelledby="interstitial-title">
  <div class="modal">
    <h2 id="interstitial-title">Before you continue</h2>
    <p>We have updated our processing terms. Nothing is required of you; this
       notice can be dismissed.</p>
    <button type="button" data-testid="interstitial-dismiss"
            onclick="document.querySelector('[data-testid=interstitial-modal]').remove()">
      Dismiss
    </button>
  </div>
</div>"""

    body = f"""
{modal}
<h1>Your profile</h1>
<p class="sub">Signed in as <span data-testid="session-email">{esc(session_email)}</span></p>
{_errors_block(errors)}
<form method="post" action="/profile{qs}" data-testid="profile-form">
  {_hidden_flags(active)}
  <div class="field">
    <label for="full-name">Name</label>
    <input type="text" id="full-name" name="full_name"
           value="{esc(values.get('full_name', ''))}" data-testid="profile-full-name">
  </div>
  <div class="field">
    <!-- Same visible label as the field above, no test id, and the association
         is via aria-labelledby rather than for=. Locating by visible text alone
         is ambiguous here, which is the point. -->
    <span id="display-name-lbl" style="display:block;font-weight:500;
          margin:0 0 .3rem;font-size:.9rem">Name</span>
    <input type="text" id="display-name" name="display_name"
           aria-labelledby="display-name-lbl"
           value="{esc(values.get('display_name', ''))}">
    <p class="hint">Shown to reviewers instead of your legal name.</p>
  </div>
  <div class="field">
    <label for="timezone">Time zone</label>
    <select id="timezone" name="timezone" data-testid="profile-timezone">{options}</select>
  </div>
  <button type="submit" data-testid="profile-continue">Save and continue</button>
</form>
"""
    return layout("Profile", body)


# ── /application ──────────────────────────────────────────────────────────────
def application_page(
    *, errors: dict[str, str], values: dict[str, str], qs: str, active: list[str],
    dependent_field: bool, delayed_element: bool, disabled_submit: bool,
    delay_ms: int, confirm_token: str | None, notice: str = "",
) -> str:
    departments = "".join(
        f'<option value="{esc(v)}"{" selected" if values.get("department") == v else ""}>{esc(t)}</option>'
        for v, t in fixtures.DEPARTMENTS
    )

    radios = "".join(
        f"""<div class="opt">
      <input type="radio" id="pref-{esc(v)}" name="contact_preference" value="{esc(v)}"
             {"checked" if values.get("contact_preference") == v else ""}>
      <label for="pref-{esc(v)}">{esc(t)}</label>
    </div>"""
        for v, t in fixtures.CONTACT_PREFERENCES
    )

    # Two of three checkboxes carry a test id; the third does not.
    checked = values.get("topics", "").split(",")
    boxes = ""
    for idx, (v, t) in enumerate(fixtures.NOTIFICATION_TOPICS):
        tid = _testid(f"app-topic-{v}") if idx < 2 else ""
        boxes += f"""<div class="opt">
      <input type="checkbox" id="topic-{esc(v)}" name="topics" value="{esc(v)}"
             {"checked" if v in checked else ""}{tid}>
      <label for="topic-{esc(v)}">{esc(t)}</label>
    </div>"""

    # Appears only once the phone field is non-empty. The server enforces the
    # same rule, so the mode is real rather than a cosmetic `hidden` attribute.
    employer_block = ""
    if dependent_field:
        employer_block = f"""
  <div class="field" id="employer-block" data-requires="phone"
       data-testid="app-employer-block" hidden>
    <label for="employer">Current employer</label>
    <input type="text" id="employer" name="employer"
           value="{esc(values.get('employer', ''))}" data-testid="app-employer">
    <p class="hint">Required once a telephone number is supplied.</p>
  </div>
  <script>
    (function () {{
      var phone = document.getElementById('phone');
      var block = document.getElementById('employer-block');
      function sync() {{ block.hidden = phone.value.trim() === ''; }}
      phone.addEventListener('input', sync);
      sync();
    }})();
  </script>"""

    # Not in the initial HTML at all — fetched from this origin once the server
    # agrees enough time has passed. A test that reads the DOM immediately finds
    # nothing, which is the behaviour worth practising against.
    delayed_block = ""
    if delayed_element:
        delayed_block = f"""
  <div id="consent-slot" data-testid="consent-slot"
       data-appear-after-ms="{delay_ms}" data-src="/application/consent-panel"></div>
  <script>
    (function () {{
      var slot = document.getElementById('consent-slot');
      setTimeout(function () {{
        fetch(slot.dataset.src, {{ credentials: 'same-origin' }})
          .then(function (r) {{ return r.ok ? r.text() : ''; }})
          .then(function (html) {{ if (html) slot.innerHTML = html; }});
      }}, {delay_ms});
    }})();
  </script>"""

    # Server-rendered as disabled; script enables it once the required fields
    # are non-empty. The server re-checks on POST regardless — a disabled
    # attribute is a hint to a human, never a control.
    submit_attrs = ' disabled aria-disabled="true"' if disabled_submit else ""
    enabler = ""
    if disabled_submit:
        enabler = """
  <script>
    (function () {
      var form = document.querySelector('[data-testid=application-form]');
      var btn = document.querySelector('[data-testid=app-submit]');
      var required = ['applicant_name', 'contact_email', 'department', 'start_date'];
      function sync() {
        var ok = required.every(function (n) {
          var el = form.elements[n];
          return el && String(el.value).trim() !== '';
        });
        var pref = form.elements['contact_preference'];
        ok = ok && !!(pref && pref.value);
        btn.disabled = !ok;
        btn.setAttribute('aria-disabled', String(!ok));
      }
      form.addEventListener('input', sync);
      form.addEventListener('change', sync);
      sync();
    })();
  </script>"""

    confirm_field = (
        f'<input type="hidden" name="confirm_token" value="{esc(confirm_token)}">'
        if confirm_token
        else ""
    )
    notice_block = (
        f'<div class="notice" data-testid="page-notice">{esc(notice)}</div>' if notice else ""
    )

    body = f"""
<h1>Application</h1>
<p class="sub">All fields are processed by the lab and recorded verbatim.</p>
{notice_block}
{_errors_block(errors)}
<form method="post" action="/application{qs}" enctype="multipart/form-data"
      data-testid="application-form" novalidate>
  {_hidden_flags(active)}
  {confirm_field}

  <fieldset>
    <legend>Applicant</legend>
    <div class="field">
      <label for="applicant-name">Full name</label>
      <input type="text" id="applicant-name" name="applicant_name"
             value="{esc(values.get('applicant_name', ''))}" data-testid="app-name">
    </div>
    <div class="field">
      <label for="reference-a">Reference</label>
      <input type="text" id="reference-a" name="reference_a"
             value="{esc(values.get('reference_a', ''))}" data-testid="app-reference">
      <p class="hint">Internal requisition code.</p>
    </div>
    <div class="field">
      <!-- Duplicated visible label, no test id, aria-labelledby association. -->
      <span id="reference-b-lbl" style="display:block;font-weight:500;
            margin:0 0 .3rem;font-size:.9rem">Reference</span>
      <input type="text" id="reference-b" name="reference_b"
             aria-labelledby="reference-b-lbl"
             value="{esc(values.get('reference_b', ''))}">
      <p class="hint">Name of a person who can vouch for you.</p>
    </div>
  </fieldset>

  <fieldset>
    <legend>Contact</legend>
    <div class="field">
      <label for="contact-email">Email address</label>
      <input type="email" id="contact-email" name="contact_email"
             value="{esc(values.get('contact_email', ''))}" data-testid="app-email">
    </div>
    <div class="field">
      <!-- No test id and no for=; the only association is aria-labelledby. -->
      <span id="phone-lbl" style="display:block;font-weight:500;
            margin:0 0 .3rem;font-size:.9rem">Telephone</span>
      <input type="tel" id="phone" name="phone" aria-labelledby="phone-lbl"
             value="{esc(values.get('phone', ''))}">
    </div>
{employer_block}
    <fieldset>
      <!-- Radio inputs carry no test ids: the group is reachable by legend,
           each option only by its for=/id label. -->
      <legend>How should we reach you?</legend>
      {radios}
    </fieldset>
  </fieldset>

  <fieldset>
    <legend>Role</legend>
    <div class="field">
      <label for="department">Department</label>
      <select id="department" name="department" data-testid="app-department">{departments}</select>
    </div>
    <div class="field">
      <label for="start-date">Earliest start date</label>
      <input type="date" id="start-date" name="start_date"
             value="{esc(values.get('start_date', ''))}" data-testid="app-start-date">
    </div>
    <div class="field">
      <label for="resume">Attach your CV</label>
      <input type="file" id="resume" name="resume" data-testid="app-resume">
    </div>
  </fieldset>

  <fieldset>
    <legend>Keep me posted about</legend>
    {boxes}
  </fieldset>
{delayed_block}
  <button type="submit" data-testid="app-submit"{submit_attrs}>Submit application</button>
</form>
{enabler}
"""
    return layout("Application", body)


def consent_panel() -> str:
    """The fragment the delayed slot fetches. Same origin, no assets."""
    return """<fieldset data-testid="consent-panel">
  <legend>Consent</legend>
  <div class="opt">
    <input type="checkbox" id="consent" name="consent" value="yes" data-testid="app-consent">
    <label for="consent">I confirm the information given is accurate.</label>
  </div>
</fieldset>"""


# ── confirmation step (the dialog, not the page) ──────────────────────────────
def confirm_step_page(
    *, values: dict[str, str], files: list[dict], token: str, qs: str, active: list[str],
) -> str:
    """Rendered *instead of* accepting the submit when confirm_dialog is on.

    An in-DOM step rather than a native `confirm()`: a native dialog is not in
    the accessibility tree the worker inspects, so it would test the dialog
    handler rather than the DOM strategy. The token echoed here is what the
    server requires to accept the real submit, so the step cannot be skipped by
    posting directly.
    """
    rows = "".join(
        f"<dt>{esc(k)}</dt><dd>{esc(str(v)) if v else '—'}</dd>"
        for k, v in values.items()
        if not k.startswith("_")
    )
    if files:
        names = ", ".join(f["filename"] for f in files)
        rows += f"<dt>attachment</dt><dd>{esc(names)}</dd>"

    hidden = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">'
        for k, v in values.items()
        if not k.startswith("_")
    )

    body = f"""
<h1>Please confirm</h1>
<p class="sub">Nothing has been submitted yet.</p>
<div class="modal-backdrop" data-testid="confirm-dialog" role="dialog" aria-modal="true"
     aria-labelledby="confirm-title">
  <div class="modal">
    <h2 id="confirm-title">Submit this application?</h2>
    <dl class="summary" data-testid="confirm-summary">{rows}</dl>
    <form method="post" action="/application{qs}" style="margin-top:1.25rem"
          data-testid="confirm-form">
      {_hidden_flags(active)}
      {hidden}
      <input type="hidden" name="confirm_token" value="{esc(token)}">
      <button type="submit" data-testid="confirm-accept">Yes, submit</button>
      <a href="/application{qs}"><button type="button" class="secondary"
         data-testid="confirm-cancel">Go back</button></a>
    </form>
  </div>
</div>
"""
    return layout("Confirm", body)


# ── /confirmation ─────────────────────────────────────────────────────────────
def confirmation_page(*, submission_id: str, values: dict[str, str]) -> str:
    rows = "".join(
        f"<dt>{esc(k)}</dt><dd>{esc(str(v)) if v else '—'}</dd>"
        for k, v in values.items()
        if not k.startswith("_")
    )
    body = f"""
<h1>Application received</h1>
<p class="sub">Reference
   <strong data-testid="submission-id">{esc(submission_id)}</strong></p>
<p>Thank you. A copy of what we received is shown below, and is also available
   to tests at <code>/_test/submissions</code>.</p>
<dl class="summary" data-testid="confirmation-summary">{rows}</dl>
"""
    return layout("Confirmation", body)


# ── /careers — deliberately off the happy path ────────────────────────────────
def careers_page() -> str:
    """Reachable, real, and irrelevant.

    §10.3 asks for a page that is not in the happy path so navigation limits
    have something to be tested against. It links onward to further lab pages
    so an agent that starts wandering can keep wandering, and the budget rather
    than a dead end is what stops it.
    """
    body = """
<h1>Open roles</h1>
<p class="sub">Not part of the application flow.</p>
<p>This page exists so navigation limits have somewhere to be exceeded. It is
   linked from every page and leads nowhere useful.</p>
<ul>
  <li><a href="/careers?role=ops" data-testid="careers-ops">Operations Analyst</a></li>
  <li><a href="/careers?role=fin" data-testid="careers-fin">Finance Associate</a></li>
  <li><a href="/careers?role=legal" data-testid="careers-legal">Compliance Officer</a></li>
</ul>
"""
    return layout("Open roles", body)
