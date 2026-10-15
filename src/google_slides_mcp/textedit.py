"""Replace an element's text with the smallest edit, so review comments stay anchored.

The obvious way to set a shape's text, ``deleteText ALL`` then ``insertText``, deletes
every character a comment is anchored to; the thread stays open but detached from the
slide (see :mod:`comments`). Instead we diff old text against new word by word and
touch only the spots that changed, so a comment on words that survive keeps its anchor
even when other parts of the text were rewritten. Where a changed spot replaces text
wholesale, the new text is inserted *inside* the old before the old is trimmed away,
which keeps an anchor that covered the old text attached to the new. (``replaceAllText``
and edits inside an anchored range keep anchors too; delete-and-retype does not.)

The edit is index-based, so it is written with the revision it was computed from; if
the deck changed in between, it is recomputed once.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from googleapiclient.errors import HttpError

from .client import Services, _status_of, error_message, execute
from .comments import find_element, text_of, utf16_len


# Words, runs of whitespace, and single punctuation or symbol characters.
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
# Beyond this many token pairs, don't diff word by word (it is quadratic); edit one block.
_MAX_TOKEN_PAIRS = 2_000_000


def _affixes(old: str, new: str) -> tuple[int, int]:
    """Lengths of the common prefix and (non-overlapping) common suffix."""
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    return prefix, suffix


def _offsets(tokens: list[str]) -> list[int]:
    offsets = [0]
    for token in tokens:
        offsets.append(offsets[-1] + len(token))
    return offsets


def changed_spots(old: str, new: str) -> list[tuple[int, str, str]]:
    """The spots where ``new`` differs from ``old``: ``(offset in old, old text, new text)``.

    In text order, offsets in code points. Found by a word-level diff, then trimmed
    to the characters that really differ inside each changed word.
    """
    old_tokens, new_tokens = _TOKEN.findall(old), _TOKEN.findall(new)
    blocks: list[tuple[int, str, str]] = []
    if len(old_tokens) * len(new_tokens) > _MAX_TOKEN_PAIRS:
        prefix, suffix = _affixes(old, new)
        blocks.append((prefix, old[prefix : len(old) - suffix], new[prefix : len(new) - suffix]))
    else:
        old_at, new_at = _offsets(old_tokens), _offsets(new_tokens)
        matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                blocks.append((old_at[i1], old[old_at[i1] : old_at[i2]], new[new_at[j1] : new_at[j2]]))
    spots = []
    for at, was, now in blocks:
        prefix, suffix = _affixes(was, now)
        was, now = was[prefix : len(was) - suffix], now[prefix : len(now) - suffix]
        if was or now:
            spots.append((at + prefix, was, now))
    return spots


def edit_requests(
    object_id: str, old: str, new: str, cell: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    """The requests that turn ``old`` into ``new`` with the least text touched.

    ``old`` and ``new`` exclude the shape's final newline, which cannot be deleted.
    Returns ``[]`` when they are equal. Indexes are UTF-16 code units. Spots are
    edited last to first, so the offsets of the earlier ones stay valid.
    """

    def where() -> dict[str, Any]:
        return {"objectId": object_id, **({"cellLocation": cell} if cell else {})}

    def insert(at: int, text: str) -> dict[str, Any]:
        return {"insertText": {**where(), "insertionIndex": at, "text": text}}

    def delete(start: int, end: int) -> dict[str, Any]:
        return {
            "deleteText": {
                **where(),
                "textRange": {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end},
            }
        }

    requests: list[dict[str, Any]] = []
    for at, was, now in reversed(changed_spots(old, new)):
        start = utf16_len(old[:at])
        end = start + utf16_len(was)
        if not was:
            requests.append(insert(start, now))
        elif not now:
            requests.append(delete(start, end))
        elif len(was) == 1:
            requests += [delete(start, end), insert(start, now)]
        else:
            # Insert inside the old text, then trim the old from both sides (tail first
            # so the head's indexes stay valid).
            first = utf16_len(was[0])
            added = utf16_len(now)
            requests += [
                insert(start + first, now),
                delete(start + first + added, end + added),
                delete(start, start + first),
            ]
    return requests


def _current_text(
    element: dict[str, Any], element_id: str, row: int | None, column: int | None
) -> tuple[str, dict[str, int] | None]:
    """The element's text without its final newline, and the cell to address (tables)."""
    table = element.get("table")
    if table:
        if row is None or column is None:
            raise ValueError(
                f"Element {element_id!r} is a table: pass `row` and `column` (0-based) "
                "to choose a cell."
            )
        try:
            cell = table["tableRows"][row]["tableCells"][column]
        except (IndexError, KeyError, TypeError):
            raise ValueError(
                f"Table {element_id!r} has no cell at row {row}, column {column}."
            ) from None
        old, location = text_of(cell.get("text")), {"rowIndex": row, "columnIndex": column}
    elif "shape" in element:
        if row is not None or column is not None:
            raise ValueError(f"Element {element_id!r} is not a table; omit `row` and `column`.")
        old, location = text_of(element["shape"].get("text")), None
    else:
        raise ValueError(f"Element {element_id!r} holds no text.")
    return (old[:-1] if old.endswith("\n") else old), location


def _revision_changed(error: HttpError) -> bool:
    return _status_of(error) == 400 and "does not match the latest revision" in error_message(error)


def set_element_text(
    services: Services,
    presentation_id: str,
    element_id: str,
    text: str,
    row: int | None = None,
    column: int | None = None,
) -> dict[str, Any]:
    """Make an element's text equal ``text`` with a minimal edit (2 API calls).

    Reads the deck once, then writes the edit guarded by the revision it was computed
    from; if the deck changed in between, reads and writes once more.
    """
    if (row is None) != (column is None):
        raise ValueError("Pass both `row` and `column` (0-based), or neither.")
    attempt = 0
    while True:
        attempt += 1
        presentation = execute(
            services.slides.presentations().get(presentationId=presentation_id)
        )
        element = find_element(presentation, element_id)
        if element is None:
            raise ValueError(f"Element {element_id!r} not found in presentation.")
        old, cell = _current_text(element, element_id, row, column)
        requests = edit_requests(element_id, old, text, cell)
        if not requests:
            return {"objectId": element_id, "length": len(text), "unchanged": True}
        body: dict[str, Any] = {"requests": requests}
        if presentation.get("revisionId"):
            body["writeControl"] = {"requiredRevisionId": presentation["revisionId"]}
        try:
            execute(
                services.slides.presentations().batchUpdate(
                    presentationId=presentation_id, body=body
                )
            )
        except HttpError as error:
            if attempt == 1 and _revision_changed(error):
                continue
            raise
        return {"objectId": element_id, "length": len(text)}
