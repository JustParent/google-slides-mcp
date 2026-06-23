"""Collision-safe object-ID generation for Slides batch requests.

Slides object IDs must be unique within a presentation and match
``[a-zA-Z0-9_-]`` with length 5-50. ``createSlide``/``createShape``/
``duplicateObject`` all accept an optional explicit ``objectId``; we generate one
when the caller does not supply theirs so new elements can be referenced
immediately in follow-up calls.
"""

from __future__ import annotations

import secrets
import string

_ALPHABET = string.ascii_lowercase + string.digits
# Slides allows 5-50 chars; keep a comfortable margin under 50.
_MAX_LEN = 50


def new_id(prefix: str = "g") -> str:
    """Generate a unique, API-valid object ID with an optional prefix.

    Args:
        prefix: A short, human-readable hint (e.g. ``"slide"``, ``"shape"``).
            Sanitized to the allowed character set.

    Returns:
        An ID like ``"slide_k3f9d1a8b2c4"`` (always <= 50 chars).
    """
    clean = "".join(c for c in prefix.lower() if c in _ALPHABET) or "g"
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    candidate = f"{clean}_{suffix}"
    return candidate[:_MAX_LEN]


def remap(object_ids: list[str], prefix: str = "copy") -> dict[str, str]:
    """Build a ``duplicateObject.objectIds`` map assigning fresh IDs.

    Useful when duplicating an object and you want predictable, non-colliding IDs
    for its children.

    Args:
        object_ids: The source child object IDs to remap.
        prefix: Prefix for the generated replacement IDs.

    Returns:
        A mapping of ``{source_id: new_id}``.
    """
    return {oid: new_id(prefix) for oid in object_ids}
