"""Builds the Google API service clients and provides a bounded retry wrapper.

All Google API calls in this server go through :func:`execute` so that transient
``429``/``5xx`` responses are retried with bounded exponential backoff + jitter.
The retry count is capped so a throttled call fails fast rather than hanging — a
key part of keeping every tool's cost finite.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from functools import lru_cache

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .auth import load_credentials

# Backoff schedule: ~1, 2, 4, 8, 16s (with jitter), capped retries.
_MAX_RETRIES = 5
_BASE_DELAY = 1.0
_MAX_DELAY = 16.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class Services:
    """Holds the built Slides and Drive service clients."""

    slides: object
    drive: object


@lru_cache(maxsize=1)
def get_services() -> Services:
    """Build (and cache) the Slides v1 and Drive v3 service clients.

    Cached for the lifetime of the process so credentials are loaded once.
    """
    creds = load_credentials()
    slides = build("slides", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return Services(slides=slides, drive=drive)


def _status_of(error: HttpError) -> int | None:
    status = getattr(getattr(error, "resp", None), "status", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def execute(request):
    """Execute a googleapiclient request with bounded backoff on transient errors.

    Args:
        request: A googleapiclient request object (has an ``.execute()`` method).

    Returns:
        The decoded response.

    Raises:
        HttpError: re-raised unchanged for non-retryable errors or once retries
            are exhausted.
    """
    last_error: HttpError | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as error:
            status = _status_of(error)
            if status not in _RETRYABLE_STATUS or attempt == _MAX_RETRIES:
                raise
            last_error = error
            delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
            # Full jitter avoids synchronized retries (thundering herd).
            time.sleep(delay * (0.5 + random.random() / 2))
    # Unreachable, but keeps type-checkers happy.
    assert last_error is not None
    raise last_error
