"""
Neo4j driver lifecycle — one driver per process.

The driver is a connection *pool*, not a connection: thread-safe, expensive to
build, meant to be created once and shared. Building one per request is the
classic misuse, so this module is the only place `GraphDatabase.driver()` is
called; everything else goes through `get_driver()`.

The pool is built LAZILY rather than at import. Neo4j is additive infrastructure
in this phase, so importing `backend.graph` must never be able to fail — an
unconfigured or dead graph has to leave the rest of the API untouched.
`verify_connectivity()` therefore returns a bool and logs; only callers that
actively want the graph get an exception (GraphUnavailable).
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import AuthError, ConfigurationError, Neo4jError, ServiceUnavailable

from config.settings import (
    NEO4J_CONNECT_TIMEOUT,
    NEO4J_DATABASE,
    NEO4J_ENABLED,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USERNAME,
)

log = logging.getLogger("aganeti.graph.client")

_driver: Optional[Driver] = None
_lock = threading.Lock()


class GraphUnavailable(RuntimeError):
    """Raised when a caller needs the graph but it is disabled or unreachable.

    Carries the URI (never the password) so an operator can see which target
    failed without grepping config."""

    def __init__(self, message: str, uri: str = NEO4J_URI) -> None:
        super().__init__(f"{message} (uri={uri})")
        self.uri = uri


def is_enabled() -> bool:
    """False when NEO4J_ENABLED=false, or URI/password are unset."""
    return bool(NEO4J_ENABLED and NEO4J_URI and NEO4J_PASSWORD)


def get_driver() -> Driver:
    """Return the process-wide driver, building it on first use.

    Raises GraphUnavailable when the graph is disabled or the driver cannot be
    constructed (bad URI scheme, missing credentials)."""
    global _driver
    if not is_enabled():
        raise GraphUnavailable(
            "Neo4j is disabled or unconfigured — set NEO4J_ENABLED=true and NEO4J_PASSWORD")
    if _driver is not None:
        return _driver
    with _lock:
        if _driver is not None:          # another thread won the race
            return _driver
        try:
            _driver = GraphDatabase.driver(
                NEO4J_URI,
                auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
                connection_timeout=NEO4J_CONNECT_TIMEOUT,
                # Modest pool: a side-car store, not the request hot path.
                max_connection_pool_size=20,
                connection_acquisition_timeout=30.0,
                # Without this the server sends an INFORMATION notification for
                # every `IF NOT EXISTS` statement that was already satisfied, and
                # the driver logs each one at INFO — ~11 paragraphs of noise on
                # every single boot. Warnings and errors still come through.
                notifications_min_severity="WARNING",
            )
        except (ConfigurationError, ValueError) as e:
            raise GraphUnavailable(f"could not build Neo4j driver: {e}") from e
        log.info("Neo4j driver created for %s (database=%s)", NEO4J_URI, NEO4J_DATABASE)
    return _driver


def verify_connectivity() -> bool:
    """Probe the server. True when reachable; logs and returns False otherwise.

    Deliberately non-raising — startup calls this, and a graph outage must not
    take the API down."""
    if not is_enabled():
        log.info("Neo4j disabled (NEO4J_ENABLED=false or no password) — skipping probe")
        return False
    try:
        drv = get_driver()
        drv.verify_connectivity()
        info = drv.get_server_info()
        log.info("Neo4j reachable: %s at %s (protocol %s)",
                 info.agent, NEO4J_URI, ".".join(str(p) for p in info.protocol_version))
        return True
    except AuthError:
        log.error("Neo4j authentication FAILED for user %r at %s — check NEO4J_USERNAME / "
                  "NEO4J_PASSWORD. Graph features stay disabled.", NEO4J_USERNAME, NEO4J_URI)
    except (ServiceUnavailable, GraphUnavailable) as e:
        log.warning("Neo4j unreachable at %s (%s). Graph features stay disabled; the rest "
                    "of the API is unaffected.", NEO4J_URI, e)
    except Neo4jError as e:
        log.warning("Neo4j probe failed at %s: %s", NEO4J_URI, e)
    except Exception as e:  # noqa: BLE001 — a probe must never propagate
        log.warning("Neo4j probe error at %s: %s", NEO4J_URI, e)
    return False


def close_driver() -> None:
    """Close the pool. Idempotent — safe on shutdown even if never opened."""
    global _driver
    with _lock:
        if _driver is not None:
            try:
                _driver.close()
                log.info("Neo4j driver closed")
            except Exception as e:  # noqa: BLE001
                log.warning("error closing Neo4j driver: %s", e)
            finally:
                _driver = None


def database() -> str:
    """The database every session targets."""
    return NEO4J_DATABASE
