"""Version-keyed cache for the read models.

The rule the master plan (§16 S11) sets is: *a page reads a prepared answer; it
never adds up documents itself.*  That only stays true if the prepared answers
are cheap to produce.  This module is a tiny in-process cache keyed by
``(name, filters…)`` and invalidated by ``ledger_version``:

* every posting and reversal bumps the ledger version (see ``ledger.posting``);
* a cached value carries the version it was built at;
* a read after any posting sees a different version, so it rebuilds;
* nothing is invalidated by hand, so no caller can forget to.

It is deliberately process-local: correctness comes from the version check, not
from the cache.  Multiple workers simply keep their own copies.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("app.cache")

__all__ = ["get_or_build", "invalidate", "stats", "reset"]

_registry: dict[tuple, tuple] = {}
_lock = threading.RLock()
_hits = 0
_misses = 0
MAX_ENTRIES = 512


def _prune() -> None:
    if len(_registry) <= MAX_ENTRIES:
        return
    # Oldest-built first: read models are small, so this is enough.
    ordered = sorted(_registry.items(), key=lambda item: item[1][2])
    for key, _ in ordered[: max(1, len(ordered) // 4)]:
        _registry.pop(key, None)


def _scope() -> str:
    """Identify the database this process is serving.

    Two app instances (tests, a restored snapshot, a second deployment) can hold
    the same ledger version number, so the version alone is not a safe cache key.
    """
    try:
        from flask import current_app

        return str(current_app.config.get("APP_DB_PATH") or "")
    except Exception:
        return ""


def get_or_build(key: tuple, version, builder):
    """Return the cached value built at *version*, or build and remember it."""
    global _hits, _misses
    key = (_scope(),) + tuple(key)
    with _lock:
        entry = _registry.get(key)
        if entry is not None and entry[0] == version:
            _hits += 1
            return entry[1]
    _misses += 1
    value = builder()
    with _lock:
        _registry[key] = (version, value, time.time())
        _prune()
    return value


def invalidate(prefix=None) -> int:
    """Drop cached values (optionally only those whose key starts with *prefix*)."""
    with _lock:
        if prefix is None:
            count = len(_registry)
            _registry.clear()
            return count
        keys = [key for key in _registry if str(key[0]).startswith(str(prefix))]
        for key in keys:
            _registry.pop(key, None)
        return len(keys)


def stats() -> dict:
    with _lock:
        return {"entries": len(_registry), "hits": _hits, "misses": _misses}


def reset() -> None:
    global _hits, _misses
    with _lock:
        _registry.clear()
        _hits = _misses = 0
