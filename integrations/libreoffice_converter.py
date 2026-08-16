import os
import signal
import asyncio
import uuid
import shutil
from pathlib import Path


# Minimum plausible size for a converted PDF. LibreOffice can emit a structurally
# valid but contentless PDF; anything under this is not a document.
MIN_PDF_BYTES = 1024

#: Magic bytes we require of a converted PDF. Checked because the return code
#: cannot be trusted — see verify_conversion.
PDF_MAGIC = b"%PDF-"


#: Share of characters that may be Unicode private-use / replacement / unmapped
#: before extracted text is judged to be mojibake rather than a document.
GARBAGE_RATIO = 0.10


def looks_like_document_text(text: str) -> tuple[bool, str]:
    """Is this extracted text a document, or is it binary noise wearing a PDF?

    THIS IS THE HALF THAT statting THE FILE DOES NOT COVER, and it is the half
    that matters. Measured on this box: a .doc containing 24 KB of random bytes
    converts with exit 0 to a structurally VALID PDF — right magic bytes, right
    size — from which pdfplumber then extracts 14,422 characters of mojibake:

        '쇑쵐绷ϧ㰇\ueffeꡙ㯂Ԅ芀罻啲狠િ(cid:2)կ\ue8ee捸錊…'

    Every artefact-level check passes. Only the content betrays it. Ingested,
    that becomes ~10 chunks of noise embedded into the shared corpus, retrievable
    forever, indistinguishable from a real document at query time — the same
    shape as recording status="indexed" with 0 chunks, except worse, because
    here the poison is searchable.

    Two signals, both cheap and both specific to broken glyph mapping rather
    than to unfamiliar languages:

      * Unicode PRIVATE USE AREA and U+FFFD REPLACEMENT characters. Real text in
        any script uses assigned codepoints; a font-mapping failure emits PUA.
      * pdfplumber's `(cid:N)` marker, which it writes when a glyph has no
        mapping at all.

    Deliberately NOT a check on script or language: Arabic, Chinese and Korean
    documents are all expected here, and "looks foreign" is not "looks broken".
    The Chinese business-licence scan in data_vault is exactly the case a
    naive ASCII-ratio check would have thrown away.

    Returns (ok, reason) — the reason is written for a user, not a log.
    """
    stripped = "".join(text.split())
    if not stripped:
        return False, "no text could be extracted"

    def _suspect(ch: str) -> bool:
        o = ord(ch)
        return (0xE000 <= o <= 0xF8FF          # private use area
                or 0xF0000 <= o <= 0x10FFFD    # supplementary private use
                or o == 0xFFFD)                # replacement character

    bad = sum(1 for ch in stripped if _suspect(ch))
    ratio = bad / len(stripped)
    if ratio > GARBAGE_RATIO:
        return False, (f"the converted text is {ratio:.0%} unmapped glyphs — the "
                       f"file is probably corrupt or not really a document")

    # (cid:N) survives as literal text; six of them in a short extract is enough.
    cids = text.count("(cid:")
    if cids and cids > max(5, len(stripped) // 500):
        return False, (f"the converted text contains {cids} unmapped glyph markers "
                       f"— the file's fonts could not be read")

    return True, "ok"


class ConversionFailed(RuntimeError):
    """A conversion that did not produce a usable document.

    Distinct from TimeoutError so callers can tell "took too long" from
    "produced rubbish" — they warrant different messages to a user.
    """


def verify_conversion(expected: Path, *, returncode: int | None = None,
                      stdout: bytes = b"", stderr: bytes = b"") -> Path:
    """Decide whether a LibreOffice conversion actually succeeded.

    THE RETURN CODE IS NOT A SUCCESS SIGNAL. Measured on this box
    (LibreOffice 24.2.7.2):

      * a .docx containing 24 KB of random bytes  -> exit 0, and 24 KB of binary
        garbage written out as "text". LibreOffice falls through to its Text
        import filter and cheerfully converts noise.
      * a valid ZIP with a truncated document.xml -> exit 0, no output file,
        "Error: source file could not be loaded" printed to STDOUT.
      * a truncated .docx                          -> exit 0, no output file.

    So both failure directions pass a returncode check, and one of them yields
    plausible-looking bytes. That is the same shape as the ingest bug where a
    file was recorded status="indexed" with 0 chunks: a success that is not one,
    which then poisons whatever consumes it.

    Success is therefore judged by the ARTEFACT: it exists, it is big enough to
    be a document, and it starts with the format's magic bytes. The return code
    and stderr are used only to make the error message useful.
    """
    detail = ""
    if returncode not in (0, None):
        detail = f" (exit {returncode})"
    err = (stderr or b"").decode("utf-8", "replace").strip()
    out = (stdout or b"").decode("utf-8", "replace").strip()
    # LibreOffice reports load failures on STDOUT, not stderr, while still
    # exiting 0 — so both streams are worth quoting back.
    hint = err or out
    if hint:
        detail += f": {hint[:200]}"

    if not expected.exists():
        raise ConversionFailed(f"LibreOffice produced no output file{detail}")

    size = expected.stat().st_size
    if size < MIN_PDF_BYTES:
        raise ConversionFailed(
            f"LibreOffice produced a {size}-byte file, too small to be a "
            f"document{detail}")

    with expected.open("rb") as fh:
        head = fh.read(len(PDF_MAGIC))
    if head != PDF_MAGIC:
        raise ConversionFailed(
            f"LibreOffice output is not a PDF (starts with {head!r}){detail}")

    return expected


async def convert_to_pdf_safe(input_path: Path, output_dir: Path, timeout: float = 30.0) -> Path:
    """
    Converts a document to PDF with concurrency isolation, timeouts, and process group cleanup.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
        
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate unique user installation path to enable concurrent conversions
    unique_id = uuid.uuid4().hex
    user_install_dir = Path(f"/tmp/libreoffice_env_{unique_id}")
    user_install_dir.mkdir(parents=True, exist_ok=True)

    # CLI args enforcing headless, isolated environment, and target output
    cmd = [
        "soffice",
        f"-env:UserInstallation=file://{user_install_dir.as_posix()}",
        "--headless",
        "--convert-to", "pdf",
        "--outdir", output_dir.as_posix(),
        input_path.as_posix()
    ]
    
    process = None
    try:
        # Start the process in a new process group to allow reliable SIGKILL on timeouts
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid  # Creates process group
        )
        
        # Enforce execution timeout limit
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        
        # NOTE: returncode is captured for the error message only. It is not the
        # success test — verify_conversion below explains why it cannot be.
        
    except asyncio.TimeoutError as te:
        if process:
            # Terminate the entire process group (including any child threads spawned by soffice)
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception as e:
                print(f"[ERROR] Failed to kill hanging LibreOffice process group {process.pid}: {e}")
        raise TimeoutError(f"Document conversion timed out after {timeout} seconds.") from te
        
    finally:
        # Cleanup isolated environment directory off-thread
        if user_install_dir.exists():
            await asyncio.to_thread(shutil.rmtree, user_install_dir, ignore_errors=True)

    expected_pdf = output_dir / f"{input_path.stem}.pdf"
    return verify_conversion(expected_pdf, returncode=process.returncode if process else None,
                             stdout=stdout, stderr=stderr)
