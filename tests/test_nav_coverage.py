"""Every route must have a navigation entry.

Two pages shipped reachable only by typing their URL on a phone. Observability
and Drafts existed in the sidebar's array and not in the bottom nav's, because
there were two hand-maintained arrays and nothing compared them. Nobody noticed
until someone opened the app on a phone and looked for a tab.

The arrays are now one list (frontend/src/lib/navItems.ts) with placement
declared per item, which stops the two navs drifting. This test closes the other
half: a route that exists but is in NO list at all.

`desktopOnly` is an allowed answer — but it has to be typed into the item, which
is the point. The failure mode being prevented is silence, not desktop-only pages.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "frontend" / "src" / "App.tsx"
NAV = ROOT / "frontend" / "src" / "lib" / "navItems.ts"

# Routes that are legitimately not destinations.
NON_NAV_ROUTES = {
    "*",        # the catch-all / not-found element
    "/login",   # pre-auth; the nav does not render at all there
}


def _routes() -> set[str]:
    return set(re.findall(r'<Route path="([^"]+)"', APP.read_text())) - NON_NAV_ROUTES


def _nav_entries() -> dict[str, str]:
    """{route: placement} from the single source of truth."""
    src = NAV.read_text()
    body = src[src.index("export const NAV_ITEMS"):src.index("/** Sidebar order")]
    return {m.group(1): m.group(2)
            for m in re.finditer(r"to: '([^']+)'.*?placement: '(\w+)'", body, re.S | re.M)}


def test_every_route_has_a_nav_entry():
    routes, nav = _routes(), _nav_entries()
    missing = routes - set(nav)
    assert not missing, (
        f"route(s) with no NAV_ITEMS entry: {sorted(missing)}. Add them to "
        f"frontend/src/lib/navItems.ts — placement 'desktopOnly' is a valid answer, "
        f"but it has to be stated or the page is unreachable on mobile.")


def test_no_nav_entry_points_at_a_missing_route():
    """The reverse: a tab that 404s is worse than a missing tab."""
    routes, nav = _routes(), _nav_entries()
    orphans = set(nav) - routes
    assert not orphans, f"NAV_ITEMS entries with no route in App.tsx: {sorted(orphans)}"


def test_the_two_pages_this_was_written_for_are_reachable_on_mobile():
    """Regression guard, named. Both were sidebar-only and URL-only on a phone."""
    nav = _nav_entries()
    for route in ("/observability", "/drafts"):
        assert route in nav, route
        assert nav[route] in ("primary", "overflow"), \
            f"{route} is {nav[route]} — unreachable on mobile again"


def test_placements_are_from_the_declared_set():
    valid = {"primary", "overflow", "desktopOnly"}
    bad = {r: p for r, p in _nav_entries().items() if p not in valid}
    assert not bad, f"unknown placement(s): {bad}"


def test_the_bottom_bar_stays_thumb_sized():
    """Primary tabs share one row with the "More" button. Past five the touch
    target drops below the 44px the rest of the bar keeps."""
    primary = [r for r, p in _nav_entries().items() if p == "primary"]
    assert len(primary) <= 5, f"{len(primary)} primary tabs + More is too many: {primary}"


def test_neither_nav_defines_its_own_list():
    """The drift this whole change removes. A local array in either component
    re-creates it."""
    for name in ("Sidebar.tsx", "BottomNav.tsx"):
        src = (ROOT / "frontend" / "src" / "components" / name).read_text()
        assert "navItems" in src, f"{name} must read the shared list"
        assert not re.search(r"^const \w+ *(?::[^=]+)?= \[\s*\n\s*\{ to:", src, re.M), \
            f"{name} defines its own nav array again"


def test_the_parse_found_something():
    """A regex that matched nothing would make every test above vacuous."""
    assert len(_routes()) >= 6, _routes()
    assert len(_nav_entries()) >= 6, _nav_entries()
