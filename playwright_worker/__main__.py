"""Run the worker straight from the repo venv, for local Level 1 work.

    python -m playwright_worker

The container entrypoint is uvicorn on the module directly (see Dockerfile); this
exists so the worker can be driven without Docker, the same way `python -m
browser_lab` runs the lab.
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("playwright_worker.service:app",
                host=os.getenv("WORKER_HOST", "127.0.0.1"),
                port=int(os.getenv("WORKER_PORT", "8090")),
                workers=1, log_level=os.getenv("WORKER_LOG_LEVEL", "info"))
