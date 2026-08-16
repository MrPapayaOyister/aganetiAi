"""Per-turn chat attachments: a file attached to THIS conversation.

Deliberately not the same thing as the Files tab, which ingests into the
permanent knowledge base. The distinction is the whole point of this module:

    paperclip  -> this conversation, ephemeral, verbatim in context
    Files tab  -> permanent corpus, semantic retrieval via search_knowledge

The old paperclip did neither honestly. It POSTed to /ingest/upload (so the file
went into the KNOWLEDGE BASE, not the conversation), then synthesised a chat
message reading `I've uploaded "X" — please acknowledge.` The model received the
FILENAME and nothing else, so "I've acknowledged the upload of X" was the only
reply it could give, and the follow-up "what was in that document" correctly
answered that it had no access. Both the toast ("indexed", while indexing is
async) and the reply asserted success at opposite ends of a gap.

WHAT REACHES THE MODEL
----------------------
Extracted text goes into the MESSAGE CONTENT, never the system prompt. The system
prompt is shared and already 2,428 tokens; an attachment is per-turn and must not
be baked into a cached preamble.

Budget, measured with the real Qwen3 tokenizer against a 40,960-token window:

    system prompt        2,428
    tool schemas         4,834   (34 tools, as templated)
    history (p50)           61
    completion reserve     700
    = fixed              8,043

That leaves ~32,900, but two caps bind first: LiteLLM's 30s deadline on
`qwen-fast` (a full-window prompt needs ~34.5s of prefill+decode) and
MAX_TOOL_ROUNDS=4, which re-sends everything up to five times. Hence a
deliberately conservative ATTACHMENT_TOKEN_BUDGET well below the headroom.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("aria.attachments")

#: Hard ceiling on an uploaded attachment. Nothing today enforces ANY size limit
#: on uploads — not uvicorn, not starlette (its 1MB cap applies to non-file parts
#: only), not a proxy — so a 200MB body is read whole into memory today. 25MB is
#: comfortably above a long PDF and far below anything that threatens a box at
#: 110/121 GB.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

#: Tokens of attachment text allowed into a turn. See the module docstring for
#: the arithmetic. Chars are estimated at 4/token, which the measurement showed
#: is accurate to 0.5% for prose and conservative for everything else.
ATTACHMENT_TOKEN_BUDGET = 12_000
ATTACHMENT_CHAR_BUDGET = ATTACHMENT_TOKEN_BUDGET * 4

#: Characters of an attachment quoted back on FOLLOW-UP turns, when the full text
#: is no longer injected. Enough to keep the model oriented, small enough that
#: several attachments in a thread stay affordable.
FOLLOWUP_CHAR_BUDGET = 2_000


# ── what we accept ───────────────────────────────────────────────────────────
#
# The BACKEND decides. `accept=` in the composer is a picker hint the user can
# trivially bypass, and today it disagrees with reality in both directions: it
# offers .doc (which produced zero chunks and was reported "indexed") and omits
# .xlsx/.csv, which work.
#
# Extension AND content must agree. An extension alone is a claim by the
# uploader; magic bytes are evidence. A .exe renamed to .pdf is the obvious case,
# but the one that actually happened here is duller and worse: random bytes named
# .doc converted "successfully" and yielded 14,422 characters of mojibake.

#: Magic-byte signatures, by container. Written out rather than pulled from
#: python-magic/filetype: the set is five signatures, all stable for decades, and
#: a dependency for that is a poor trade.
_SIG_PDF = b"%PDF-"
_SIG_ZIP = b"PK\x03\x04"          # docx, xlsx, pptx, odt, ods, odp
_SIG_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # legacy doc, xls, ppt
_SIG_RTF = b"{\\rtf"
_SIG_PNG = b"\x89PNG\r\n\x1a\n"
_SIG_JPEG = b"\xff\xd8\xff"          # SOI + first marker; covers JFIF and EXIF
#: WEBP is the one format here that is NOT a simple prefix: "RIFF", then a
#: 4-byte length, then "WEBP". Checking only "RIFF" would also accept a WAV or
#: an AVI renamed to .webp, which is exactly the class of thing this map exists
#: to reject, so it gets a predicate instead of a prefix.
def _is_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"

#: extension -> the signature its bytes must start with, a PREDICATE for formats
#: whose magic is not a prefix, or None for plain text (validated by decoding).
ACCEPTED: dict[str, "bytes | None | Callable[[bytes], bool]"] = {
    ".pdf": _SIG_PDF,
    ".docx": _SIG_ZIP, ".xlsx": _SIG_ZIP, ".pptx": _SIG_ZIP,
    ".odt": _SIG_ZIP, ".ods": _SIG_ZIP, ".odp": _SIG_ZIP,
    ".doc": _SIG_OLE2, ".xls": _SIG_OLE2, ".ppt": _SIG_OLE2,
    ".rtf": _SIG_RTF,
    ".txt": None, ".md": None, ".csv": None,
    # Images. Read as OCR text plus a one-line type description — see
    # backend/services/image_read.py. They were in NOT_YET until the OCR path
    # moved to PaddleOCR, which reads them natively.
    ".png": _SIG_PNG, ".jpg": _SIG_JPEG, ".jpeg": _SIG_JPEG,
    ".webp": _is_webp,
}

#: Formats a user will plausibly try that we knowingly do not handle YET. Named
#: so the rejection can say "not yet" rather than "unsupported", which is the
#: difference between a roadmap and a dead end.
NOT_YET = {
    # png/jpg/jpeg/webp moved to ACCEPTED when OCR moved to PaddleOCR. These
    # three remain: gif is usually animated, heic needs a decoder we do not
    # ship, and bmp is rare enough not to have been measured.
    ".gif": "images", ".heic": "images", ".bmp": "images",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".ogg": "audio",
    ".mp4": "video", ".mov": "video", ".webm": "video",
}


class AttachmentRejected(Exception):
    """Refused before storage. `reason` is written for the user, verbatim."""

    def __init__(self, reason: str, *, status: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def check_upload(data: bytes, filename: str) -> str:
    """Validate size, extension and CONTENT. Returns the normalised extension.

    Raises AttachmentRejected with a reason a user can act on — never a generic
    "unsupported file". Every branch here names what was wrong and, where it can,
    what would work instead.
    """
    name = Path(filename or "").name
    ext = Path(name).suffix.lower()

    if not data:
        raise AttachmentRejected("That file is empty.")

    if len(data) > MAX_ATTACHMENT_BYTES:
        mb = len(data) / (1024 * 1024)
        raise AttachmentRejected(
            f"That file is {mb:.1f} MB. The limit for chat attachments is "
            f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB — for something larger, "
            f"add it to Files instead.",
            status=413)

    if not ext:
        raise AttachmentRejected(
            "That file has no extension, so I can't tell what it is. "
            "Rename it with the right one (.pdf, .docx, .txt) and try again.")

    if ext in NOT_YET:
        what = NOT_YET[ext]
        # The message has to stay true as formats graduate. Once PNG/JPEG/WebP
        # became readable, "I can't read images yet" was simply false for a .gif
        # — and a wrong reason sends someone looking in the wrong place, which
        # is the failure every message in this module is written against.
        if what == "images":
            raise AttachmentRejected(
                f"I can't read {ext.lstrip('.')} images yet — try PNG, JPEG or "
                f"WebP, which I can read.")
        raise AttachmentRejected(
            f"I can't read {what} yet — only documents (PDF, Word, Excel, text) "
            f"and images (PNG, JPEG, WebP). Support for {what} is planned.")

    if ext not in ACCEPTED:
        raise AttachmentRejected(
            f"I can't read {ext} files. Attach a PDF, Word, Excel, CSV or text "
            f"file instead.")

    sig = ACCEPTED[ext]
    if callable(sig):
        if not sig(data):
            raise AttachmentRejected(
                f"That file is named {ext} but its contents are not a "
                f"{ext.lstrip('.')} file. It may be renamed, corrupt, or a "
                f"different format.")
        return ext
    if sig is None:
        # No signature to check, so prove it is text by decoding it. A binary
        # renamed to .txt would otherwise sail through and be embedded as noise.
        try:
            data[:65536].decode("utf-8")
        except UnicodeDecodeError:
            try:
                data[:65536].decode("utf-16")
            except UnicodeDecodeError:
                raise AttachmentRejected(
                    f"That {ext} file isn't readable text — it looks like binary "
                    f"data with a text extension.") from None
        return ext

    if not data.startswith(sig):
        raise AttachmentRejected(
            f"That file is named {ext} but its contents are not a "
            f"{ext.lstrip('.')} file. It may be renamed, corrupt, or a different "
            f"format.")
    return ext


# ── extraction ───────────────────────────────────────────────────────────────

@dataclass
class Attachment:
    """One file attached to one turn."""

    filename: str
    ext: str
    size: int
    text: str
    truncated: bool = False
    total_chars: int = 0
    chunks: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        kb = self.size / 1024
        n = f"{self.total_chars:,} characters"
        return (f"{self.filename} ({kb:.0f} KB, {n}"
                + (", truncated" if self.truncated else "") + ")")


def extract(data: bytes, filename: str, ext: str) -> Attachment:
    """Bytes -> text, reusing the ingest extractor so behaviour cannot diverge.

    Writes to a temp file because extract_file_text is path-based; that is also
    what gives attachments the LibreOffice legacy-format path and the PDF OCR
    fallback for free, rather than growing a second extraction stack.
    """
    import shutil
    import tempfile

    from backend.ingest import extract_file_text

    work = Path(tempfile.mkdtemp(prefix="aganeti-attach-"))
    try:
        target = work / (Path(filename).name or f"upload{ext}")
        target.write_bytes(data)
        text = extract_file_text(target) or ""
    finally:
        shutil.rmtree(work, ignore_errors=True)

    total = len(text)
    truncated = total > ATTACHMENT_CHAR_BUDGET
    kept = text[:ATTACHMENT_CHAR_BUDGET] if truncated else text
    return Attachment(filename=Path(filename).name, ext=ext, size=len(data),
                      text=kept, truncated=truncated, total_chars=total,
                      chunks=_chunks(text) if truncated else [])


def _chunks(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Paragraph-aware slices for the over-budget case.

    Split on blank lines first so a chunk rarely begins mid-sentence; fall back to
    a hard slice for prose with no paragraph breaks.
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= size:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                out.append(buf)
            buf = p if len(p) <= size else ""
            if not buf:
                for i in range(0, len(p), size - overlap):
                    out.append(p[i:i + size])
    if buf:
        out.append(buf)
    return out


# ── what goes into the message ───────────────────────────────────────────────

_OPEN = "<<<ATTACHMENT"
_CLOSE = ">>>"


def context_block(att: Attachment, *, full: bool) -> str:
    """The text appended to a user turn for this attachment.

    Delimited rather than blended into the user's sentence so the model can tell
    the document from the question, and so a document containing instructions is
    visibly data rather than something addressed to it.

    `full=True` on the turn the file was attached: the whole text, up to budget.
    `full=False` afterwards: a header plus an excerpt. Re-sending the full text
    every turn would cost the budget again on each of up to five tool rounds,
    which is what makes a long thread with an attachment hit the 30s deadline.
    """
    head = f"{_OPEN} {att.filename} {_CLOSE}"
    tail = f"{_OPEN} end of {att.filename} {_CLOSE}"
    if full:
        note = ""
        if att.truncated:
            note = (f"\n[Only the first {len(att.text):,} of {att.total_chars:,} "
                    f"characters are shown.]")
        return f"\n\n{head}\n{att.text}{note}\n{tail}"

    excerpt = att.text[:FOLLOWUP_CHAR_BUDGET]
    more = "" if len(att.text) <= FOLLOWUP_CHAR_BUDGET else " …"
    return (f"\n\n{head} (attached earlier in this conversation; "
            f"{att.total_chars:,} characters)\n{excerpt}{more}\n{tail}")


def budget_report(atts: list[Attachment]) -> dict:
    """What the attachments cost this turn, for logging and the UI."""
    chars = sum(len(a.text) for a in atts)
    return {"count": len(atts), "chars": chars,
            "approx_tokens": chars // 4,
            "budget_tokens": ATTACHMENT_TOKEN_BUDGET,
            "over_budget": chars > ATTACHMENT_CHAR_BUDGET}
