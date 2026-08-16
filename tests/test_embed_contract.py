"""The embed field contract, across the language boundary.

backend/tool_result.py builds an embed dict; frontend/src/hooks/useStream.ts maps
the SSE payload into an EmbedItem with a HAND-WRITTEN whitelist. Nothing connects
them, so a field the backend starts emitting is simply absent on the client until
someone notices the feature not working.

That is not hypothetical. `channel_kind` was added to the backend, persisted,
returned on rehydrate, typed in EmbedItem — and omitted from this one mapper. The
effect was invisible in review and specific: a station ticked in the picker
committed as a TV channel, failed HLS validation, and looked like a bad station.

This test reads both files and compares the key sets. It is crude — a regex over
source in another language — but it is the only thing in the repo that would have
caught that, and it fails at the moment the divergence is introduced rather than
when a user reports the symptom.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_RESULT = ROOT / "backend" / "tool_result.py"
USE_STREAM = ROOT / "frontend" / "src" / "hooks" / "useStream.ts"
# The SECOND way an embed reaches the client: /chat/history rebuilds one from the
# stored spec. It does NOT pass through the useStream whitelist, so the tests
# above cannot see it — and it has already shipped this same bug once. Its own
# comment records channels/channel_kind/articles/csp being persisted and never
# returned, which lost the picker and the links on reload.
MAIN = ROOT / "backend" / "main.py"

# Emitted by the backend but deliberately NOT carried to the client. Each entry
# needs a reason, because "unused" is how the channel_kind bug looked too.
BACKEND_ONLY = {
    # The QR regeneration spec. Consumed by backend/main.py when rebuilding the
    # card from chat_artifacts; the browser never reads it.
    "qr",
}


def _backend_embed_keys() -> set[str]:
    src = TOOL_RESULT.read_text()
    start = src.index("embeds.append({")
    end = src.index("})", start)
    return set(re.findall(r'"([a-z_]+)":', src[start:end]))


def _client_mapped_keys() -> set[str]:
    src = USE_STREAM.read_text()
    start = src.index("const items: EmbedItem[]")
    end = src.index("}))", start)
    block = src[start:end]
    # Object-literal keys at the start of a line inside the .map() body.
    return set(re.findall(r"^\s{20,}([a-zA-Z_]+):", block, re.M))


def test_the_client_carries_every_field_the_backend_emits():
    backend = _backend_embed_keys()
    client = _client_mapped_keys()
    missing = backend - client - BACKEND_ONLY
    assert not missing, (
        f"useStream.ts drops embed field(s) the backend emits: {sorted(missing)}. "
        f"Add them to the mapper, or to BACKEND_ONLY here with a reason.")


def test_the_client_does_not_invent_fields():
    """The reverse drift: a key the client maps that no longer exists server-side
    is dead code that reads as a working feature."""
    backend = _backend_embed_keys()
    client = _client_mapped_keys()
    extra = client - backend
    assert not extra, f"useStream.ts maps field(s) the backend never sends: {sorted(extra)}"


def test_the_parse_actually_found_something():
    """A regex that silently matches nothing would make both tests above pass
    forever. Pin the shape so a refactor breaks this rather than the guarantee."""
    backend = _backend_embed_keys()
    client = _client_mapped_keys()
    assert len(backend) >= 8, backend
    assert len(client) >= 8, client
    for expected in ("html", "channels", "channel_kind"):
        assert expected in backend, expected
        assert expected in client or expected in BACKEND_ONLY, expected


# ── the second surface: rehydration from /chat/history ───────────────────────
# Live SSE and reload are different code paths that must agree on the same
# EmbedItem. The whitelist above guards one of them; nothing guarded this one.

#: Present only on a rehydrated embed, so legitimately absent from the SSE shape.
REHYDRATE_ONLY = {
    # "as of" chip — by definition a snapshot has one and a live lookup does not.
    "renderedAt",
    # HTML exceeded the storage cap and only the structured half was kept.
    "oversized",
}

#: Emitted over SSE but consumed SERVER-side when rebuilding, never re-returned:
#: main.py regenerates the card from `spec["qr"]` and returns the html instead.
CONSUMED_ON_REHYDRATE = {"qr"}


def _rehydrated_embed_keys() -> set[str]:
    src = MAIN.read_text()
    start = src.index("out.append({")
    end = src.index("})", start)
    return set(re.findall(r'"([a-zA-Z_]+)":', src[start:end]))


def test_a_reloaded_embed_keeps_every_field_a_live_one_has():
    """The reload half of the channel_kind bug. A field that survives the SSE
    mapper but is dropped here works until the user refreshes, which is the
    hardest version of this bug to reproduce."""
    live = _backend_embed_keys()
    rehydrated = _rehydrated_embed_keys()
    missing = live - rehydrated - CONSUMED_ON_REHYDRATE
    assert not missing, (
        f"/chat/history rebuilds an embed without field(s) the live path sends: "
        f"{sorted(missing)}. The widget will differ after a reload. Add them to "
        f"the dict in backend/main.py, or to CONSUMED_ON_REHYDRATE with a reason.")


def test_rehydration_adds_only_declared_snapshot_fields():
    """The reverse: a field invented on reload that the live path never sends is
    either dead or a divergence in the other direction."""
    live = _backend_embed_keys()
    extra = _rehydrated_embed_keys() - live - REHYDRATE_ONLY
    assert not extra, (
        f"/chat/history returns field(s) no live embed has: {sorted(extra)}. "
        f"Add to REHYDRATE_ONLY with a reason, or stop sending them.")


def test_the_rehydration_parse_actually_found_something():
    keys = _rehydrated_embed_keys()
    assert len(keys) >= 8, keys
    for expected in ("html", "channels", "channel_kind", "renderedAt"):
        assert expected in keys, expected
