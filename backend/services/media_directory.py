"""
The iptv-org directory: a daily cache of upstream, used to find channels by
name, category or country without pasting a URL.

A CACHE, not a library. Channels the user actually keeps live in
media_sources; nothing here is theirs until they pick it and it validates.

Two upstream files, no key and no rate limit — static JSON on GitHub Pages:
    channels.json   ~10 MB, 41,037 entries — mostly a reference catalogue
    streams.json    ~3.7 MB, 17,832 entries

Structural filtering happens on the way IN, so every read is already safe:

  * `closed` and `is_nsfw` channels dropped;
  * `http://` streams dropped — mixed content on an https page;
  * streams carrying `user_agent` or `referrer` dropped. This is the subtle one:
    a browser's fetch cannot set either, so such a stream PASSES a server-side
    validation and then dies in the player — exactly the "works at add time,
    fails at play time" shape we refuse to ship.

Measured: 41,037 channels reduce to ~8,457 structurally usable, of which about
half are actually live at any moment. Liveness is checked separately, at search
and again at add, because it changes by the hour and cannot be cached.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("aria.media_directory")

SOURCE = "iptv-org"
CHANNELS_URL = "https://iptv-org.github.io/api/channels.json"
STREAMS_URL = "https://iptv-org.github.io/api/streams.json"
FETCH_TIMEOUT = 90

# A refresh producing fewer than this is treated as FAILED and discarded. The
# real number is ~8,457; a truncated download or an upstream schema change would
# otherwise quietly empty the directory, which is worse than serving stale data.
MIN_PLAUSIBLE_ROWS = 3000

# Past this, search tells the model the directory may be out of date rather than
# pretending it is fresh.
STALE_AFTER_DAYS = 7


def _usable_stream(s: dict) -> bool:
    url = s.get("url") or ""
    return (url.startswith("https://")
            and not s.get("user_agent")
            and not s.get("referrer"))


def build_rows(channels: list[dict], streams: list[dict]) -> list[dict]:
    """Join channels to their best usable stream, applying every structural
    filter. Pure — no network, no database — so it is directly testable."""
    best: dict[str, dict] = {}
    for s in streams:
        cid = s.get("channel")
        if cid and _usable_stream(s) and cid not in best:
            best[cid] = s

    rows = []
    for c in channels:
        s = best.get(c.get("id"))
        if not s or c.get("closed") or c.get("is_nsfw"):
            continue
        rows.append({
            "source": SOURCE,
            "ext_id": c["id"],
            "name": (c.get("name") or "").strip(),
            "alt_names": [a for a in (c.get("alt_names") or []) if a],
            "country": c.get("country"),
            "categories": c.get("categories") or [],
            "url": s["url"],
            "quality": s.get("quality"),
        })
    return [r for r in rows if r["name"] and r["url"]]


def refresh() -> tuple[bool, str]:
    """Rebuild the directory. Returns (ok, message).

    Never truncates before the new data is in hand and has passed the sanity
    gate: a stale directory is far better than an empty one, and the failure
    mode of DELETE-then-INSERT is exactly an empty one.
    """
    from backend.chat import store as chat_store
    if not chat_store.enabled():
        return False, "database disabled"

    try:
        import httpx
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as c:
            channels = c.get(CHANNELS_URL).json()
            streams = c.get(STREAMS_URL).json()
    except Exception as exc:
        msg = f"fetch failed: {type(exc).__name__}: {exc}"
        _record_failure(msg)
        log.warning("directory refresh: %s — keeping the existing index", msg)
        return False, msg

    try:
        rows = build_rows(channels, streams)
    except Exception as exc:
        msg = f"parse failed: {type(exc).__name__}: {exc}"
        _record_failure(msg)
        log.warning("directory refresh: %s — keeping the existing index", msg)
        return False, msg

    if len(rows) < MIN_PLAUSIBLE_ROWS:
        msg = (f"refusing to replace the directory: only {len(rows)} usable rows "
               f"(expected at least {MIN_PLAUSIBLE_ROWS}) — upstream may be "
               f"truncated or its schema may have changed")
        _record_failure(msg)
        log.warning("directory refresh: %s", msg)
        return False, msg

    try:
        from backend.db import models as M
        from backend.db import sync as dbsync
        with dbsync.session() as s:
            # One transaction: the delete and the insert commit together, so a
            # crash between them cannot leave the table empty.
            s.query(M.MediaDirectory).filter(
                M.MediaDirectory.source == SOURCE).delete(synchronize_session=False)
            s.bulk_insert_mappings(M.MediaDirectory, rows)
            state = s.query(M.MediaDirectoryState).filter_by(source=SOURCE).one_or_none()
            if state is None:
                state = M.MediaDirectoryState(source=SOURCE)
                s.add(state)
            state.last_refreshed_at = datetime.now(timezone.utc)
            state.last_error = None
            state.row_count = len(rows)
            s.commit()
        log.info("directory refresh: %d channels cached", len(rows))
        return True, f"cached {len(rows)} channels"
    except Exception as exc:
        msg = f"write failed: {type(exc).__name__}: {exc}"
        _record_failure(msg)
        log.exception("directory refresh: write failed — existing index left in place")
        return False, msg


def _record_failure(message: str) -> None:
    """Record why a refresh failed WITHOUT touching the rows it failed to
    replace."""
    try:
        from backend.db import models as M
        from backend.db import sync as dbsync
        with dbsync.session() as s:
            state = s.query(M.MediaDirectoryState).filter_by(source=SOURCE).one_or_none()
            if state is None:
                state = M.MediaDirectoryState(source=SOURCE)
                s.add(state)
            state.last_error = message[:500]
            s.commit()
    except Exception:
        log.exception("directory refresh: could not record the failure")


def status() -> dict:
    """Freshness of the cached directory."""
    try:
        from backend.db import models as M
        from backend.db import sync as dbsync
        with dbsync.session() as s:
            st = s.query(M.MediaDirectoryState).filter_by(source=SOURCE).one_or_none()
            if st is None:
                return {"rows": 0, "age_days": None, "stale": True, "error": None}
            age = None
            if st.last_refreshed_at:
                age = (datetime.now(timezone.utc) - st.last_refreshed_at).days
            return {"rows": st.row_count or 0, "age_days": age,
                    "stale": age is None or age > STALE_AFTER_DAYS,
                    "error": st.last_error}
    except Exception:
        log.exception("directory status failed")
        return {"rows": 0, "age_days": None, "stale": True, "error": None}


def search(name: str = "", category: str = "", country: str = "",
           limit: int = 20) -> list[dict]:
    """Candidate channels, best match first. Filters combine — "UAE news"
    narrows to a handful.

    Returns MORE than will be shown: the caller validates these and keeps the
    ones that are actually live, and roughly half are not.
    """
    try:
        from sqlalchemy import Text, cast, or_, select

        from backend.db import models as M
        from backend.db import sync as dbsync
        q = select(M.MediaDirectory).where(M.MediaDirectory.source == SOURCE)
        name = (name or "").strip()
        if name:
            like = f"%{name}%"
            # alt_names is what makes "BBC1" find "BBC One".
            # cast(): alt_names is a JSONB array, and matching inside it as text
            # is what makes "BBC1" find "BBC One".
            q = q.where(or_(M.MediaDirectory.name.ilike(like),
                            cast(M.MediaDirectory.alt_names, Text).ilike(like)))
        if country:
            q = q.where(M.MediaDirectory.country == country.strip().upper())
        if category:
            q = q.where(cast(M.MediaDirectory.categories, Text)
                        .ilike(f"%{category.strip().lower()}%"))
        with dbsync.session() as s:
            rows = s.execute(q.limit(max(1, min(int(limit), 60)))).scalars().all()
            out = [{"ext_id": r.ext_id, "name": r.name, "country": r.country,
                    "categories": r.categories or [], "url": r.url,
                    "quality": r.quality} for r in rows]
    except Exception:
        log.exception("directory search failed")
        return []

    if name:
        # Exact and prefix matches first — "add Al Arabiya" should not lead with
        # "Al Arabiya Business" when "Al Arabiya" itself exists.
        low = name.lower()
        out.sort(key=lambda r: (r["name"].lower() != low,
                                not r["name"].lower().startswith(low),
                                len(r["name"])))
    return out
