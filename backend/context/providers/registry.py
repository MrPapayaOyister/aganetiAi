"""
Provider registry — the extension point.

`ContextBuilder` iterates whatever is registered here. Adding a source is:

    from backend.context.providers import register, ContextProvider

    class SlackProvider(ContextProvider):
        name = "slack"
        async def collect(self, request): ...

    register(SlackProvider())

No change to ContextBuilder, ContextBundle or the prompt builder is required —
an unrecognised provider name lands in `bundle.extra` automatically.

Order of registration is preserved, which matters only for deterministic stats
and log output: providers run concurrently, so it does not affect latency.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from .base import ContextProvider

log = logging.getLogger("aganeti.context.registry")

_REGISTRY: "dict[str, ContextProvider]" = {}


def register(provider: ContextProvider, *, replace: bool = False) -> ContextProvider:
    """Add a provider. Re-registering the same name is an error unless `replace`,
    so a typo in a new provider cannot silently shadow an existing source."""
    name = provider.name
    if not name or name == "provider":
        raise ValueError(f"{type(provider).__name__} must set a unique `name`")
    if name in _REGISTRY and not replace:
        raise ValueError(f"context provider {name!r} is already registered")
    _REGISTRY[name] = provider
    log.debug("context: registered provider %s", name)
    return provider


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def get(name: str) -> Optional[ContextProvider]:
    return _REGISTRY.get(name)


def all_providers(only: Optional[Iterable[str]] = None) -> "list[ContextProvider]":
    """Registered providers, optionally narrowed to `only` (order preserved)."""
    if only is None:
        return list(_REGISTRY.values())
    wanted = list(only)
    return [_REGISTRY[n] for n in wanted if n in _REGISTRY]


def names() -> "list[str]":
    return list(_REGISTRY)


def clear() -> None:
    """Test helper — drops every registration."""
    _REGISTRY.clear()
