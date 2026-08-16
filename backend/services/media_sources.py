"""
The media source library: live TV channels and (later) news feeds.

A LIBRARY, global + per-user, deliberately not per-session. Tool toggles answer
"may this chat use Live TV at all"; this answers "which channels exist". You
curate a channel list once and expect it everywhere, so a session scope would be
the wrong shape — and unlike toggles it must not vary thread to thread.

Validation happens at ADD time, not play time — a stream that plays fine in a
normal tab but fails only inside our sandbox is the worst failure we could ship,
because it looks like our bug and reproduces nowhere else.

WHAT is checked depends on `kind`, because the two players have different
requirements and each one's rules reject the other's working streams:

  tv    — HLS via hls.js, which fetches the manifest and segments by XHR. Our
          frame is opaque-origin and sends `Origin: null`, so the stream must
          answer with a permissive `Access-Control-Allow-Origin` or nothing
          loads at all. The body must also parse as a playlist.
  radio — a plain <audio> element, which loads cross-origin media with NO CORS
          header whatsoever. Requiring one here would reject most working
          stations. What it needs instead is https, not-HLS, and a genuinely
          continuous stream. See validate_radio.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("aria.media_sources")

# Seeded from Hermes's curated list. Red Bull TV is deliberately absent: their
# comment records that its endpoint rotates and the stream was failing, which is
# exactly the kind of knowledge worth carrying over rather than rediscovering.
SEED_CHANNELS = [
    {"name": "Al Jazeera English", "category": "news",
     "url": "https://live-hls-web-aje.getaj.net/AJE/index.m3u8"},
    {"name": "Al Jazeera Arabic", "category": "news",
     "url": "https://live-hls-web-aja.getaj.net/AJA/index.m3u8"},
    {"name": "DW English", "category": "news",
     "url": "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/index.m3u8"},
    {"name": "France 24 English", "category": "news",
     "url": "https://static.france24.com/live/F24_EN_LO_HLS/live_web.m3u8"},
    {"name": "NASA TV", "category": "science",
     "url": "https://ntv1.akamaized.net/hls/live/2014075/NASA-NTV1-HLS/master.m3u8"},
]

VALIDATE_TIMEOUT = 12
SEARCH_PROBE_TIMEOUT = 5


def validate_source(url: str, kind: str = "tv",
                    timeout: float | None = None) -> tuple[bool, str]:
    """Check a stream is playable FROM INSIDE OUR SANDBOX, by kind.

    `kind` selects the check because the two media paths have genuinely different
    requirements, and applying either one's rules to the other rejects working
    streams:

      tv    — HLS. hls.js fetches the manifest and every segment by XHR, and a
              sandboxed frame sends `Origin: null`, so the server MUST grant an
              opaque origin. The body must also parse as a playlist.
      radio — a continuous audio stream. A <audio> element loads cross-origin
              media with NO Access-Control-Allow-Origin header at all — verified
              in a browser — so requiring CORS here would reject most working
              stations for a rule that does not apply to them.

    Anything unknown falls through to the HLS check, which is the stricter of the
    two: a new kind that forgets to declare itself is over-validated, not under.
    """
    if (kind or "").lower() == "radio":
        return validate_radio(url, timeout)
    return validate_hls(url, timeout)


def validate_radio(url: str, timeout: float | None = None) -> tuple[bool, str]:
    """Is this a continuous audio broadcast our card can play? (ok, reason).

    Three checks, in the order they fail in practice, each one earned from a
    station that got past the previous:

      1. https — the app is served over HTTPS and the embed CSP is
         `media-src https:`, so an http:// stream is refused twice over. 40% of
         the UAE scope fails here.
      2. not .m3u8 — HLS needs MSE via hls.js, which an audio card does not load.
      3. a CONTINUOUS stream — audio content-type AND no finite Content-Length.
         Content-type alone is not enough: a station served `audio/mpeg` with
         `Content-Length: 58193`, which is ~3.4 seconds at 128kbps. It played,
         ended, and left the card reading "on air" over silence. A broadcast is
         chunked and has no length.

    The probe sends BROWSER headers deliberately. With an API-ish
    `Accept: application/json` and a bot User-Agent, one server returned
    audio/mpeg while the browser — sending `Accept: */*` — got an HTML holding
    page. A liveness check that does not ask the way the real client asks
    validates something other than what the user will get.
    """
    from backend.services import radio as _radio

    u = (url or "").strip()
    if not u.lower().startswith("https://"):
        return False, ("stream must be https — an http:// stream is blocked as mixed "
                       "content and by the player's media-src policy")
    if _radio.HLS_MARKER in u.lower():
        return False, ("HLS (.m3u8) streams need a video player; this card plays "
                       "plain audio streams only")
    if not _radio._sounds_like_audio(u):
        return False, ("did not answer as a continuous audio stream — it may be "
                       "offline, geo-blocked, or a holding page")
    return True, "ok"


def validate_hls(url: str, timeout: float | None = None) -> tuple[bool, str]:
    """Check a stream is playable FROM INSIDE OUR SANDBOX. Returns (ok, reason).

    Three things, in the order they fail in practice:

      1. reachable and 200;
      2. `Access-Control-Allow-Origin` permits an opaque origin. We send
         `Origin: null` exactly as the sandboxed frame does — a stream that
         allows a normal page but not a null origin plays everywhere except
         here, and would be blamed on us;
      3. the body actually parses as an HLS playlist, so a login page returning
         200 with HTML is rejected rather than stored as a channel.
    """
    if not url.lower().startswith(("http://", "https://")):
        return False, "url must start with http:// or https://"
    try:
        import httpx
        # Search-time probes use a shorter timeout than add-time: wall clock is
        # dominated by dead streams waiting to time out, and a manifest that has
        # not answered in a few seconds is not a channel anyone wants. The add
        # path keeps the full timeout, because that decision is durable.
        r = httpx.get(url, timeout=timeout or VALIDATE_TIMEOUT, follow_redirects=True,
                      headers={"Origin": "null", "User-Agent": "aganeti-livetv/1.0"})
    except Exception as exc:
        return False, f"unreachable ({type(exc).__name__})"

    if r.status_code != 200:
        return False, f"returned HTTP {r.status_code}"

    acao = r.headers.get("access-control-allow-origin", "")
    if acao not in ("*", "null"):
        return False, (f"stream does not allow cross-origin playback "
                       f"(Access-Control-Allow-Origin: {acao or 'absent'}) — it would "
                       f"play in a normal tab but not in the chat's sandboxed player")

    body = (r.text or "").lstrip()
    if not body.startswith("#EXTM3U"):
        return False, "does not look like an HLS playlist (no #EXTM3U)"

    return True, "ok"


# ── store ────────────────────────────────────────────────────────────────────

def _enabled() -> bool:
    from backend.chat import store as chat_store
    return chat_store.enabled()


def list_sources(user_id: str, kind: str = "tv", include_disabled: bool = False) -> list[dict]:
    """Global rows plus this user's own, newest-relevant first."""
    if not _enabled():
        return []
    try:
        from sqlalchemy import or_, select

        from backend.db import models as M
        from backend.db import sync as dbsync
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            q = select(M.MediaSource).where(
                M.MediaSource.kind == kind,
                M.MediaSource.deleted_at.is_(None),
            )
            q = q.where(or_(M.MediaSource.user_id.is_(None),
                            M.MediaSource.user_id == (user.id if user else None)))
            if not include_disabled:
                q = q.where(M.MediaSource.enabled.is_(True))
            rows = s.execute(q.order_by(M.MediaSource.name)).scalars().all()
            return [{"id": str(r.id), "name": r.name, "url": r.url,
                     "category": r.category, "seeded": r.seeded,
                     # Radio only; None for tv rows. Carried so a saved station
                     # renders the same badges as the live result it came from.
                     "codec": r.codec, "bitrate": r.bitrate,
                     "last_status": r.last_status, "mine": r.user_id is not None}
                    for r in rows]
    except Exception:
        log.exception("media sources: list failed")
        return []


def seed_defaults() -> int:
    """Insert the curated channels as global rows. Idempotent by (kind, url)."""
    if not _enabled():
        return 0
    try:
        from sqlalchemy import select

        from backend.db import models as M
        from backend.db import sync as dbsync
        added = 0
        with dbsync.session() as s:
            for ch in SEED_CHANNELS:
                exists = s.execute(
                    select(M.MediaSource).where(M.MediaSource.kind == "tv",
                                                M.MediaSource.url == ch["url"])
                ).scalars().first()
                if exists:
                    continue
                s.add(M.MediaSource(kind="tv", name=ch["name"], url=ch["url"],
                                    category=ch["category"], seeded=True, user_id=None))
                added += 1
            s.commit()
        return added
    except Exception:
        log.exception("media sources: seeding failed")
        return 0


def validate_many(urls: list[str], timeout: float = SEARCH_PROBE_TIMEOUT,
                  workers: int = 12, kind: str = "tv") -> dict[str, bool]:
    """Liveness for a batch, in parallel. Wall clock is one probe timeout, not
    the sum: dead streams dominate and they all wait concurrently.

    `kind` picks the same check the add path will apply, so what a picker shows
    as live is exactly what add will accept — a picker validated by different
    rules offers rows that then get refused."""
    from concurrent.futures import ThreadPoolExecutor
    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
        return dict(zip(urls, pool.map(lambda u: validate_source(u, kind, timeout)[0], urls)))


def add_source(user_id: str, name: str, url: str, kind: str = "tv",
               category: str = "general", codec: str | None = None,
               bitrate: int | None = None) -> tuple[bool, str]:
    """Validate then store. Returns (ok, message) — a failure is REPORTED, not
    stored with a hopeful status.

    Validation is by KIND (see validate_source). It used to run only for "tv",
    so any other kind was stored unchecked — which for radio would mean saving
    http:// and HLS URLs that can never play, and only finding out at play time.
    """
    name, url = (name or "").strip(), (url or "").strip()
    if not name or not url:
        return False, "both a name and a url are required"
    ok, reason = validate_source(url, kind)
    if not ok:
        return False, f"“{name}” was not added: {reason}"
    if not _enabled():
        return False, "the source library is unavailable (database disabled)"
    try:
        from backend.db import models as M
        from backend.db import sync as dbsync
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            if user is None:
                return False, "could not identify you, so the channel was not saved"
            s.add(M.MediaSource(
                org_id=user.org_id, user_id=user.id, kind=kind, name=name, url=url,
                category=category or "general", seeded=False,
                codec=codec or None,
                bitrate=int(bitrate) if str(bitrate or "").isdigit() else None,
                last_checked_at=datetime.now(timezone.utc), last_status="ok"))
            s.commit()
        return True, f"Added “{name}”."
    except Exception:
        log.exception("media sources: add failed")
        return False, "could not save the channel"


def remove_source(user_id: str, name: str, kind: str = "tv") -> tuple[bool, str]:
    """Soft-delete one of the user's OWN sources. Seeded globals are not
    removable by a single user — hiding them is what `enabled` is for."""
    if not _enabled():
        return False, "the source library is unavailable"
    try:
        from sqlalchemy import select

        from backend.db import models as M
        from backend.db import sync as dbsync
        with dbsync.session() as s:
            user = dbsync.resolve_user(s, user_id)
            if user is None:
                return False, "could not identify you"
            row = s.execute(
                select(M.MediaSource).where(
                    M.MediaSource.kind == kind,
                    M.MediaSource.user_id == user.id,
                    M.MediaSource.deleted_at.is_(None),
                    M.MediaSource.name.ilike(name.strip()),
                )
            ).scalars().first()
            if row is None:
                return False, f"you have no channel called “{name}” (seeded channels can be disabled, not deleted)"
            row.deleted_at = datetime.now(timezone.utc)
            s.commit()
        return True, f"Removed “{name}”."
    except Exception:
        log.exception("media sources: remove failed")
        return False, "could not remove the channel"
