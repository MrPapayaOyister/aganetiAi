import os
import signal
import asyncio
import uuid
import shutil
from pathlib import Path

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
        
        if process.returncode != 0:
            raise RuntimeError(
                f"LibreOffice returned exit code {process.returncode}. "
                f"Stderr: {stderr.decode(errors='ignore')}"
            )
            
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
    if not expected_pdf.exists():
        raise FileNotFoundError("PDF file was not created by the conversion process.")
        
    return expected_pdf
