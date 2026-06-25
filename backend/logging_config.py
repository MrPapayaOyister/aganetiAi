"""
Structured logging with per-request trace IDs.

Replaces ad-hoc print() debugging with a configured logger: a rotating file
(logs/app.log) plus stdout, every line tagged with a trace_id so a single chat turn
(chat -> tool -> endpoint) can be followed end-to-end. The trace id is carried in a
ContextVar and set per HTTP request by middleware in main.py.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

from config.settings import LOGS_DIR

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_configured = False


def set_trace_id(tid: str) -> None:
    _trace_id.set(tid)


def get_trace_id() -> str:
    return _trace_id.get()


class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id.get()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(trace_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)

    fh = RotatingFileHandler(str(LOGS_DIR / "app.log"), maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    fh.addFilter(_TraceFilter())

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.addFilter(_TraceFilter())

    root.addHandler(fh)
    root.addHandler(sh)
    # quiet noisy libraries a touch
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
