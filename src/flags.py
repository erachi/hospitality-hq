"""Feature flag client backed by AWS AppConfig.

Reads flags from the AppConfig Lambda extension running as a sidecar on
localhost:2772. The extension handles caching + polling, so this module
just does a local HTTP GET and parses the response.

Fail-safe: any error returns the caller's `default`, so a broken flag
service never takes down a Lambda.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_EXTENSION_PORT = 2772
_APP = os.environ.get("APPCONFIG_APP", "hospitality-hq")
_ENV = os.environ.get("APPCONFIG_ENV", "prod")
_PROFILE = os.environ.get("APPCONFIG_PROFILE", "feature-flags")

# Per-invocation cache: the extension itself caches across invocations,
# but reparsing JSON on every `is_enabled` call is wasteful. Reset once
# per second so a flag flip propagates within the Lambda warm window
# without spamming the extension.
_CACHE: dict | None = None
_CACHE_AT: float = 0.0
_CACHE_TTL_SECONDS = 1.0


def _fetch_flags() -> dict:
    url = (
        f"http://localhost:{_EXTENSION_PORT}"
        f"/applications/{_APP}/environments/{_ENV}/configurations/{_PROFILE}"
    )
    with urllib.request.urlopen(url, timeout=0.5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_flags() -> dict:
    global _CACHE, _CACHE_AT
    now = time.monotonic()
    if _CACHE is not None and (now - _CACHE_AT) < _CACHE_TTL_SECONDS:
        return _CACHE
    flags = _fetch_flags()
    _CACHE = flags
    _CACHE_AT = now
    return flags


def is_enabled(flag_name: str, default: bool = False) -> bool:
    """Return True if `flag_name` is enabled in AppConfig.

    Returns `default` on any failure (extension down, flag missing,
    malformed response). Never raises.
    """
    try:
        flags = _get_flags()
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "flag fetch failed for %s: %s; using default=%s", flag_name, exc, default
        )
        return default

    entry = flags.get(flag_name)
    if not isinstance(entry, dict):
        return default
    return bool(entry.get("enabled", default))


def _reset_cache_for_tests() -> None:
    """Testing hook — clears the per-invocation cache."""
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0
