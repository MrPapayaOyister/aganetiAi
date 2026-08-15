"""Run the lab directly: `python -m browser_lab`.

Binds 0.0.0.0:8080 so the container is reachable from sibling compose services
as http://browser-lab:8080 (§10.1).
"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "browser_lab.app:app",
        host=os.environ.get("BROWSER_LAB_HOST", "0.0.0.0"),
        port=int(os.environ.get("BROWSER_LAB_PORT", "8080")),
        log_level=os.environ.get("BROWSER_LAB_LOG_LEVEL", "info"),
    )
