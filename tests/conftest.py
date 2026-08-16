"""Suite-level fixtures and markers.

The repo audit found neither existed: `pytest.ini` declared no markers and there was
no `tests/conftest.py`, so every fixture was module-local and there was nothing to
gate slow or dependency-heavy tests on.

This file adds only what the browser work needs, and adds it in the shape the audit
recommended:

**Skips are decided at RUN time, never at collection time.** A module-level probe
that touches Chromium or the lab during collection builds process-wide singletons
before other suites can set up their own — that is exactly how a probe in
`test_graph_search_tool.py` once broke four unrelated `test_graph_retrieval` tests.
Every gate here is a fixture or an in-test call.

**Nothing here imports `backend/`.** The browser fixtures must not drag the
orchestrator into a test session that has no use for it.
"""
from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request

import pytest


def pytest_configure(config):
    """Register markers so `-m` works and unknown-mark warnings stay meaningful."""
    config.addinivalue_line(
        "markers",
        "browser: requires Chromium via Playwright. Skipped when unavailable.")
    config.addinivalue_line(
        "markers",
        "lab: requires a reachable browser-lab. Skipped when unavailable.")
    config.addinivalue_line(
        "markers",
        "slow: takes more than a couple of seconds.")
    config.addinivalue_line(
        "markers",
        "llm: drives a real model. Skipped when no LLM gateway answers.")


# ── availability probes (called from fixtures, never at import) ───────────────
_CHROMIUM_PROBE: tuple[bool, str] | None = None

#: Asks Playwright which executable THIS version expects and checks that exact path.
#: Globbing for `chromium*` is not enough: a stale bundle from an earlier Playwright
#: (chromium-1228 when 1.62 wants 1234) matches the glob and then fails at launch —
#: a hard error inside a test rather than a skip, i.e. the precise failure this probe
#: exists to prevent.
#:
#: Run in a SUBPROCESS. `sync_playwright()` starts a driver connection, and doing
#: that inside a pytest-asyncio session leaves "Task was destroyed but it is
#: pending!" and a TargetClosedError on stderr — noise that reads like a failure on
#: an otherwise green run.
_PROBE_SRC = (
    "import pathlib,sys\n"
    "from playwright.sync_api import sync_playwright\n"
    "with sync_playwright() as p: exe = p.chromium.executable_path\n"
    "print(exe if exe and pathlib.Path(exe).exists() else '')\n"
)


def _chromium_available() -> tuple[bool, str]:
    global _CHROMIUM_PROBE
    if _CHROMIUM_PROBE is not None:
        return _CHROMIUM_PROBE
    import subprocess
    import sys
    try:
        out = subprocess.run([sys.executable, "-c", _PROBE_SRC],
                             capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        _CHROMIUM_PROBE = (False, f"chromium probe failed ({type(e).__name__})")
        return _CHROMIUM_PROBE
    path = out.stdout.strip()
    if out.returncode == 0 and path:
        _CHROMIUM_PROBE = (True, "")
    else:
        _CHROMIUM_PROBE = (
            False, "chromium not installed (run `python -m playwright install chromium`)")
    return _CHROMIUM_PROBE


def _lab_url() -> str:
    return os.getenv("BROWSER_LAB_URL", "http://127.0.0.1:8080").rstrip("/")


def _lab_reachable(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{url}/_test/health", timeout=timeout) as r:
            if r.status == 200:
                return True, ""
            return False, f"lab health returned {r.status}"
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return False, f"lab unreachable at {url} ({type(e).__name__})"


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def chromium_available() -> bool:
    ok, _ = _chromium_available()
    return ok


@pytest.fixture(scope="session")
def require_chromium() -> None:
    ok, why = _chromium_available()
    if not ok:
        pytest.skip(why)


@pytest.fixture(scope="session")
def lab_url(require_chromium) -> str:
    """The lab base URL, skipping the test if nothing is listening.

    Depends on `require_chromium` so a browser test that needs both reports the
    more fundamental missing piece first.
    """
    url = _lab_url()
    ok, why = _lab_reachable(url)
    if not ok:
        pytest.skip(f"{why}. Start it with `python -m browser_lab` or "
                    f"`docker compose up -d browser-lab`.")
    return url


_LLM_PROBE: tuple[bool, str] | None = None


def _llm_available() -> tuple[bool, str]:
    """Ask the gateway for its model list. Cached for the session.

    A Level 3 test drives a real model, so on a machine with no gateway it must
    skip rather than fail — same rule as Chromium and the lab. Probed at RUN time
    from a fixture, never at collection: a module-level probe here would reach the
    network during `pytest --collect-only`.
    """
    global _LLM_PROBE
    if _LLM_PROBE is not None:
        return _LLM_PROBE
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    except Exception:  # noqa: BLE001
        pass
    base = (os.getenv("LLM_BASE_URL") or "").rstrip("/")
    if not base:
        _LLM_PROBE = (False, "LLM_BASE_URL is unset")
        return _LLM_PROBE
    req = urllib.request.Request(
        f"{base}/models",
        headers={"Authorization": f"Bearer {os.getenv('LLM_API_KEY', 'x')}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            _LLM_PROBE = (r.status == 200, "" if r.status == 200
                          else f"gateway returned {r.status}")
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        _LLM_PROBE = (False, f"no LLM gateway at {base} ({type(e).__name__})")
    return _LLM_PROBE


@pytest.fixture(scope="session")
def require_llm() -> None:
    ok, why = _llm_available()
    if not ok:
        pytest.skip(why)
