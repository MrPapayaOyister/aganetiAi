# Vendored browser assets

Files here are served verbatim at `/vendor/<name>` by vite in dev and by the
static host in production. They are **vendored on purpose, not bundled**: the
live-TV player runs inside a sandboxed `srcdoc` iframe, which cannot resolve a
relative URL (it has an opaque origin and no base), so the player references an
absolute URL on our own origin.

---

## hls.min.js

| | |
|---|---|
| **Pinned version** | **1.5.20** |
| Source | `https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.min.js` |
| SHA-256 | `d016c1230496ee59f3f5b01c16cce4cc01b5a1d3d357adec200c908b131ebe49` |
| Size | 415253 bytes |
| Used by | `backend/services/livetv.py` (the player card) |

### Why vendored rather than loaded from a CDN

Upstream (Hermes, and our original Open WebUI export) loads `hls.js@latest` from
a CDN. Two problems with that here:

1. **`@latest` is not a version.** The player would silently change under us,
   inside a frame that runs scripts.
2. It requires `script-src https://cdn.jsdelivr.net` in the video CSP profile —
   a third-party script origin inside a sandboxed-but-scripted frame. Serving it
   ourselves keeps `script-src` to our own origin.

### How to update

```bash
V=1.5.21            # the version you are moving to
curl -sSfo frontend/public/vendor/hls.min.js \
  "https://cdn.jsdelivr.net/npm/hls.js@${V}/dist/hls.min.js"
sha256sum frontend/public/vendor/hls.min.js
```

Then **update the table above** (version, SHA-256, size) and re-verify:

1. `pytest tests/test_livetv.py` — asserts the asset exists, is non-trivial in
   size, and that the player references it at the expected path.
2. Ask the assistant to play a channel and check in a real browser:
   - the srcdoc iframe still has `sandbox="allow-scripts"` and nothing more;
   - **zero CSP violations** in the console. A new hls.js that reached for an
     extra origin (a worker, a wasm blob, a telemetry endpoint) shows up here
     and nowhere else — this is the check that matters;
   - hls.js reaches `MANIFEST_PARSED` and the video actually plays.

The video CSP profile lives in `frontend/src/lib/embedWidget.ts` under
`CSP_PROFILES.video`. If a newer hls.js legitimately needs something that
profile does not grant, widen **that profile only** — never the shared default,
which every other embed (news, weather, YouTube cards) is rendered under.

> Note on headless verification: the CI/dev headless Chromium has no proprietary
> codecs (`canPlayType('video/mp4; codecs="avc1…")` returns `""`), so it can
> prove everything up to and including `MANIFEST_PARSED` but cannot decode
> H.264 HLS. Final playback has to be eyeballed in a normal browser.
