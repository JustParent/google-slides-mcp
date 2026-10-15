"""Review comments on Slides decks: read threads with their locations, and write them.

How the API models comments (they went GA on 2026-09-30):

* ``Presentation.comments`` holds the *threads*; each has an ``anchorId``.
* ``Page.commentAnchors`` (on each slide, and on its notes page) says where every
  ``anchorId`` sits: a slide, a whole element, a range of text in a shape, cells of
  a table, or speaker notes.
* Nothing comment-related is returned unless ``commentsViewMode`` is passed.

Facts verified against the live API that this module depends on:

* Speaker-notes anchors live on ``slide.slideProperties.notesPage.commentAnchors``,
  not on the slide itself.
* JSON omits zero values, so ``cellLocation: {}`` is row 0, column 0.
* Text offsets count UTF-16 code units, like every other Slides text index.
* When all of the commented text is deleted (or its shape or table is), the thread
  stays open and keeps its ``plainTextQuote``, but no page has an anchor for it any
  more: an *orphan*. ``replaceAllText``, or editing inside the range, keeps the anchor.
* ``duplicateObject`` can leave one anchor on several slides, so a thread can have
  more than one location.
* A batch containing an invalid comment request is rejected whole (HTTP 400).
  ``commentUpdateState`` can still be ``ALL_FAILED_UNKNOWN_REASON`` when the rest of
  a batch was applied but its comment changes were not saved.
* Every comment write bumps the presentation's ``revisionId``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from googleapiclient.errors import HttpError

from .client import Services, _status_of, error_message, execute

INCLUDED = "COMMENTS_VIEW_MODE_INCLUDED"
# Returns comments only when the caller may see them (INCLUDED answers 403 to a
# view-only user), so it is the safe mode for summaries that merely count them.
DEFAULT_ACCESS = "COMMENTS_VIEW_MODE_DEFAULT_FOR_CURRENT_ACCESS"
SAVE_FAILED = "ALL_FAILED_UNKNOWN_REASON"

# Bounds that keep output finite however many comments a deck has.
MAX_THREADS = 100
MAX_LOCATIONS = 10
MAX_CELLS = 25
MAX_QUOTE = 200
MAX_CANDIDATES = 8

_STATUSES = ("open", "resolved", "all")
_ACTIONS = ("", "resolve", "reopen")
_LAST = 10**9  # sorts orphans after every slide


class CommentNotSavedError(RuntimeError):
    """Google accepted the batch but did not save its comment changes."""


# --------------------------------------------------------------------------- #
# UTF-16 text offsets
# --------------------------------------------------------------------------- #
def utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units, which is how Slides counts text."""
    return len(text.encode("utf-16-le")) // 2


def slice_utf16(text: str, start: int, end: int) -> str:
    """``text[start:end]`` where the offsets are UTF-16 code units."""
    data = text.encode("utf-16-le")[2 * start : 2 * end]
    return data.decode("utf-16-le", errors="replace")


def _clip(text: str, limit: int = MAX_QUOTE) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------- #
# Walking pages and their text
# --------------------------------------------------------------------------- #
def text_of(text: dict[str, Any] | None) -> str:
    """The string a Slides ``TextContent`` indexes into (runs and auto text, in order)."""
    pieces = []
    for element in (text or {}).get("textElements", []):
        content = (element.get("textRun") or element.get("autoText") or {}).get("content")
        if content:
            pieces.append(content)
    return "".join(pieces)


def iter_elements(elements: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Every page element, descending into groups."""
    for element in elements:
        yield element
        group = element.get("elementGroup")
        if group:
            yield from iter_elements(group.get("children", []))


def notes_page_of(slide: dict[str, Any]) -> dict[str, Any]:
    return (slide.get("slideProperties") or {}).get("notesPage") or {}


def find_element(presentation: dict[str, Any], element_id: str) -> dict[str, Any] | None:
    """Find a page element on any slide, notes page, layout or master."""
    pages: list[dict[str, Any]] = []
    for slide in presentation.get("slides", []):
        pages.append(slide)
        pages.append(notes_page_of(slide))
    pages += presentation.get("layouts", [])
    pages += presentation.get("masters", [])
    for page in pages:
        for element in iter_elements(page.get("pageElements", [])):
            if element.get("objectId") == element_id:
                return element
    return None


@dataclass(frozen=True)
class TextTarget:
    """A body of text a comment can be anchored in: a shape, or one table cell."""

    element_id: str
    text: str
    row: int | None = None
    column: int | None = None


def text_targets(page: dict[str, Any]) -> list[TextTarget]:
    """Every shape and table cell on ``page`` that holds text."""
    targets: list[TextTarget] = []
    for element in iter_elements(page.get("pageElements", [])):
        object_id = element.get("objectId")
        shape = element.get("shape") or {}
        if "text" in shape:
            targets.append(TextTarget(object_id, text_of(shape["text"])))
        table = element.get("table")
        if table:
            for r, table_row in enumerate(table.get("tableRows", [])):
                for c, cell in enumerate(table_row.get("tableCells", [])):
                    if "text" in cell:
                        targets.append(TextTarget(object_id, text_of(cell["text"]), r, c))
    return targets


def notes_target(slide: dict[str, Any]) -> TextTarget | None:
    """The slide's speaker-notes shape, or None if the slide has none."""
    notes_page = notes_page_of(slide)
    notes_id = (notes_page.get("notesProperties") or {}).get("speakerNotesObjectId")
    for element in iter_elements(notes_page.get("pageElements", [])):
        if notes_id and element.get("objectId") == notes_id:
            return TextTarget(notes_id, text_of((element.get("shape") or {}).get("text")))
    return None


@dataclass(frozen=True)
class Match:
    """One occurrence of a quoted string; offsets are UTF-16 into ``target.text``."""

    slide_id: str
    slide_index: int
    target: TextTarget
    start: int
    end: int


def _matches_in(
    slide_id: str, slide_index: int, targets: list[TextTarget], needle: str, element_id: str | None
) -> list[Match]:
    if not needle:
        raise ValueError("`text` must not be empty.")
    found: list[Match] = []
    for target in targets:
        if element_id and target.element_id != element_id:
            continue
        position = 0
        while True:
            at = target.text.find(needle, position)
            if at < 0:
                break
            start = utf16_len(target.text[:at])
            found.append(Match(slide_id, slide_index, target, start, start + utf16_len(needle)))
            position = at + len(needle)
    return found


def find_text(
    presentation: dict[str, Any],
    needle: str,
    *,
    page_id: str | None = None,
    element_id: str | None = None,
) -> list[Match]:
    """Every occurrence of ``needle`` in the slides' shapes and table cells, in reading order."""
    if not needle:
        raise ValueError("`text` must not be empty.")
    found: list[Match] = []
    for index, slide in enumerate(presentation.get("slides", [])):
        if page_id and slide.get("objectId") != page_id:
            continue
        found += _matches_in(slide["objectId"], index, text_targets(slide), needle, element_id)
    return found


# --------------------------------------------------------------------------- #
# Reading threads
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Site:
    """One place an anchor sits: on a slide, or on that slide's speaker-notes page."""

    slide_id: str
    slide_index: int
    page: dict[str, Any]  # the slide or its notes page; its pageElements hold the text
    notes: bool
    anchor: dict[str, Any]


def anchor_sites(presentation: dict[str, Any]) -> dict[str, list[Site]]:
    """Map ``anchorId`` to every place it is anchored."""
    sites: dict[str, list[Site]] = {}
    for index, slide in enumerate(presentation.get("slides", [])):
        notes_page = notes_page_of(slide)
        for page, is_notes in ((slide, False), (notes_page, True)):
            for anchor in page.get("commentAnchors", []):
                site = Site(slide["objectId"], index, page, is_notes, anchor)
                sites.setdefault(anchor.get("anchorId"), []).append(site)
    return sites


def _range_fields(bounds: dict[str, Any], text: str) -> dict[str, Any]:
    """``start``/``end`` (UTF-16) of an API ``Range`` plus the text it covers."""
    start, end = bounds.get("startIndex", 0), bounds.get("endIndex", 0)
    fields: dict[str, Any] = {"start": start, "end": end}
    covered = slice_utf16(text, start, end)
    if covered:
        fields["text"] = _clip(covered)
    return fields


def _locations(site: Site, text_cache: dict[int, dict[tuple, str]]) -> list[dict[str, Any]]:
    texts = text_cache.setdefault(
        id(site.page),
        {(t.element_id, t.row, t.column): t.text for t in text_targets(site.page)},
    )
    base = {"slideId": site.slide_id, "slideIndex": site.slide_index}
    found: list[dict[str, Any]] = []
    for object_anchor in site.anchor.get("objectAnchors", []):
        object_id = object_anchor.get("objectId")
        if "shapeTextAnchors" in object_anchor:
            kind = "notes" if site.notes else "text"
            text = texts.get((object_id, None, None), "")
            for bounds in object_anchor["shapeTextAnchors"].get("ranges", []):
                found.append({"kind": kind, **base, "elementId": object_id, **_range_fields(bounds, text)})
        elif "tableCellAnchors" in object_anchor:
            cells: list[dict[str, Any]] = []
            for cell_range in object_anchor["tableCellAnchors"].get("cellRanges", []):
                where = cell_range.get("cellLocation") or {}
                row, column = where.get("rowIndex", 0), where.get("columnIndex", 0)
                text = texts.get((object_id, row, column), "")
                for bounds in cell_range.get("ranges") or [{}]:
                    cells.append({"row": row, "column": column, **_range_fields(bounds, text)})
            location = {"kind": "table_cells", **base, "elementId": object_id, "cells": cells[:MAX_CELLS]}
            if len(cells) > MAX_CELLS:
                location["moreCells"] = len(cells) - MAX_CELLS
            found.append(location)
        elif site.notes and object_id == site.page.get("objectId"):
            found.append({"kind": "notes", **base})
        elif object_id == site.slide_id:
            found.append({"kind": "slide", **base})
        else:
            found.append({"kind": "element", **base, "elementId": object_id})
    return found


def _author(post: dict[str, Any]) -> str:
    return (post.get("author") or {}).get("displayName") or "unknown"


def _post_view(post: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {
        "postId": post.get("postId"),
        "author": _author(post),
        "created": post.get("createTime"),
    }
    if (post.get("author") or {}).get("me"):
        view["mine"] = True
    if post.get("content"):
        view["content"] = post["content"]
    action = post.get("commentAction")
    if action in ("RESOLVE", "REOPEN"):
        view["action"] = action.lower()
    if post.get("assigneeEmail"):
        view["assignee"] = post["assigneeEmail"]
    return view


def _is_open(thread: dict[str, Any]) -> bool:
    return thread.get("status") != "RESOLVED"


def _thread_view(thread: dict[str, Any], locations: list[dict[str, Any]]) -> dict[str, Any]:
    head = _post_view(thread.get("headPost") or {})
    head.pop("postId", None)  # a thread's id is its head post's id
    view: dict[str, Any] = {
        "commentId": thread.get("commentId"),
        "status": "open" if _is_open(thread) else "resolved",
        **head,
    }
    quote = thread.get("plainTextQuote")
    if quote:
        view["quote"] = _clip(quote)
    if locations:
        view["locations"] = locations[:MAX_LOCATIONS]
        if len(locations) > MAX_LOCATIONS:
            view["moreLocations"] = len(locations) - MAX_LOCATIONS
    else:
        view["orphaned"] = True
    replies = [_post_view(p) for p in thread.get("replies", []) if not p.get("deleted")]
    if replies:
        view["replies"] = replies
    return view


def build_threads(
    presentation: dict[str, Any], *, page_id: str | None = None, status: str = "open"
) -> dict[str, Any]:
    """Turn a ``presentations.get`` response (fetched with comments) into thread views.

    Args:
        presentation: The response, fetched with ``commentsViewMode``.
        page_id: Keep only threads anchored on this slide (orphans have no slide, so
            they only appear in whole-deck listings).
        status: ``open`` (default), ``resolved`` or ``all``.

    Returns:
        ``{counts: {open, resolved, orphaned}, threads: [...]}``; ``counts`` cover every
        thread in scope regardless of ``status``, and ``orphaned`` counts open threads
        whose commented content is gone.
    """
    status = (status or "").lower()
    if status not in _STATUSES:
        raise ValueError(f"`status` must be one of {', '.join(_STATUSES)}.")
    slide_ids = [s.get("objectId") for s in presentation.get("slides", [])]
    if page_id and page_id not in slide_ids:
        raise ValueError(f"Slide {page_id!r} not found in presentation.")

    sites = anchor_sites(presentation)
    text_cache: dict[int, dict[tuple, str]] = {}
    scoped: list[tuple[tuple[int, str], dict[str, Any]]] = []
    counts = {"open": 0, "resolved": 0, "orphaned": 0}
    for thread in presentation.get("comments", []):
        thread_sites = sites.get(thread.get("anchorId"), [])
        if page_id and not any(s.slide_id == page_id for s in thread_sites):
            continue
        locations = [loc for site in thread_sites for loc in _locations(site, text_cache)]
        view = _thread_view(thread, locations)
        if _is_open(thread):
            counts["open"] += 1
            counts["orphaned"] += 0 if locations else 1
        else:
            counts["resolved"] += 1
        order = min((s.slide_index for s in thread_sites), default=_LAST)
        scoped.append(((order, view.get("created") or ""), view))

    scoped.sort(key=lambda pair: pair[0])
    wanted = [v for _, v in scoped if status == "all" or v["status"] == status]
    result: dict[str, Any] = {"counts": counts, "threads": wanted[:MAX_THREADS]}
    if len(wanted) > MAX_THREADS:
        result["threadsTruncated"] = True
    return result


def summarize(presentation: dict[str, Any]) -> dict[str, Any] | None:
    """Comment counts for the deck overview: ``{open, resolved, orphaned, bySlide}``.

    ``bySlide`` maps slide id to its number of open threads. None when the response
    carries no comments (none exist, or ``commentsViewMode`` was not passed).
    """
    threads = presentation.get("comments") or []
    if not threads:
        return None
    sites = anchor_sites(presentation)
    summary: dict[str, Any] = {"open": 0, "resolved": 0, "orphaned": 0, "bySlide": {}}
    for thread in threads:
        if not _is_open(thread):
            summary["resolved"] += 1
            continue
        summary["open"] += 1
        slide_ids = {s.slide_id for s in sites.get(thread.get("anchorId"), [])}
        if not slide_ids:
            summary["orphaned"] += 1
        for slide_id in slide_ids:
            summary["bySlide"][slide_id] = summary["bySlide"].get(slide_id, 0) + 1
    return summary


def list_comments(
    services: Services,
    presentation_id: str,
    *,
    page_id: str | None = None,
    status: str = "open",
) -> dict[str, Any]:
    """Read the deck's comment threads with their locations (1 API call)."""
    try:
        presentation = execute(
            services.slides.presentations().get(
                presentationId=presentation_id, commentsViewMode=INCLUDED
            )
        )
    except HttpError as error:
        if _status_of(error) == 403:
            raise ValueError(
                "Google refused to return comments (HTTP 403). Reading them needs "
                "comment access to the presentation; view-only access is not enough. "
                f"Google said: {error_message(error)}"
            ) from error
        raise
    result = build_threads(presentation, page_id=page_id, status=status)
    return {
        "presentationId": presentation_id,
        "revisionId": presentation.get("revisionId"),
        **result,
    }


# --------------------------------------------------------------------------- #
# Writing comments
# --------------------------------------------------------------------------- #
def _comment_batch(
    services: Services, presentation_id: str, requests: list[dict[str, Any]]
) -> dict[str, Any]:
    response = execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id, body={"requests": requests}
        )
    )
    if response.get("commentUpdateState") == SAVE_FAILED:
        raise CommentNotSavedError(
            "Google Slides accepted the request but did not save the comment change "
            f"(commentUpdateState={SAVE_FAILED}). Nothing was changed; try again."
        )
    return response


def _revision(response: dict[str, Any]) -> str | None:
    return (response.get("writeControl") or {}).get("requiredRevisionId")


def batch_summary(response: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """The tool result for a raw ``batchUpdate``: replies, new revision, comment state."""
    result: dict[str, Any] = {"replies": response.get("replies", [])}
    revision = _revision(response)
    if revision:
        result["revisionId"] = revision
    state = response.get("commentUpdateState")
    if state and state != "NO_UPDATES_REQUESTED":
        result["commentUpdateState"] = state
    if state == SAVE_FAILED:
        warnings = [
            *warnings,
            "The comment changes in this batch were NOT saved "
            f"(commentUpdateState={SAVE_FAILED}); the other requests in it were "
            "applied. Re-send only the comment requests.",
        ]
    if warnings:
        result["warnings"] = warnings
    return result


def _text_range(start: int, end: int) -> dict[str, Any]:
    return {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end}


def _anchor_for_match(match: Match, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
    target = match.target
    base = {"slideId": match.slide_id, "slideIndex": match.slide_index}
    fields = _range_fields({"startIndex": match.start, "endIndex": match.end}, target.text)
    if target.row is None:
        anchor = {
            "shapeTextAnchor": {
                "objectId": target.element_id,
                "textRange": _text_range(match.start, match.end),
            }
        }
        return anchor, {"kind": kind, **base, "elementId": target.element_id, **fields}
    anchor = {
        "tableCellTextAnchor": {
            "objectId": target.element_id,
            "cellLocation": {"rowIndex": target.row, "columnIndex": target.column},
            "textRange": _text_range(match.start, match.end),
        }
    }
    cell = {"row": target.row, "column": target.column, **fields}
    return anchor, {"kind": "table_cells", **base, "elementId": target.element_id, "cells": [cell]}


def _describe(match: Match, number: int) -> str:
    target = match.target
    where = f"slide index {match.slide_index} ({match.slide_id}), element {target.element_id}"
    if target.row is not None:
        where += f", row {target.row} column {target.column}"
    around = slice_utf16(target.text, max(0, match.start - 20), match.end + 20)
    context = _clip(" ".join(around.replace("�", "").split()), 80)
    return f"{number}. {where}: …{context}…"


def _choose(
    matches: list[Match], needle: str, occurrence: int | None, scope: str
) -> Match:
    if not matches:
        raise ValueError(f"The text {needle!r} was not found{scope}.")
    if occurrence is not None:
        if occurrence > len(matches):
            raise ValueError(
                f"occurrence={occurrence} but {needle!r} appears only {len(matches)} "
                f"time(s){scope}."
            )
        return matches[occurrence - 1]
    if len(matches) > 1:
        listed = "; ".join(_describe(m, i) for i, m in enumerate(matches[:MAX_CANDIDATES], 1))
        more = f" (and {len(matches) - MAX_CANDIDATES} more)" if len(matches) > MAX_CANDIDATES else ""
        raise ValueError(
            f"{needle!r} appears {len(matches)} times{scope}: {listed}{more}. "
            "Pass `occurrence` (1-based, in reading order) and/or `element_id` to choose."
        )
    return matches[0]


def add_comment(
    services: Services,
    presentation_id: str,
    content: str,
    *,
    page_id: str | None = None,
    element_id: str | None = None,
    text: str | None = None,
    occurrence: int | None = None,
    row: int | None = None,
    column: int | None = None,
    row_span: int | None = None,
    column_span: int | None = None,
    notes: bool = False,
) -> dict[str, Any]:
    """Start a comment thread anchored to a slide, element, text, table cells or notes.

    One API call for slide, element and table-range anchors; two (read, then write)
    when a quoted ``text`` or the speaker notes must be located first.
    """
    if not content or not content.strip():
        raise ValueError("`content` must not be empty.")
    if occurrence is not None and occurrence < 1:
        raise ValueError("`occurrence` counts from 1.")
    if (row is None) != (column is None):
        raise ValueError("Pass both `row` and `column` (0-based), or neither.")
    spans = row_span is not None or column_span is not None
    if spans and row is None:
        raise ValueError("`row_span` / `column_span` need `element_id`, `row` and `column`.")
    if spans and text is not None:
        raise ValueError("A comment on a block of table cells can't also quote `text`.")
    if row is not None and not element_id:
        raise ValueError("`row` / `column` need the table's `element_id`.")
    if notes and (element_id or row is not None):
        raise ValueError("`notes` comments take only `page_id` (and optionally `text`).")
    if notes and not page_id:
        raise ValueError("`notes=True` needs the slide's `page_id`.")

    location: dict[str, Any]
    if notes or text is not None:
        presentation = execute(services.slides.presentations().get(presentationId=presentation_id))
        if notes:
            slide_index, slide = next(
                ((i, s) for i, s in enumerate(presentation.get("slides", [])) if s.get("objectId") == page_id),
                (None, None),
            )
            if slide is None:
                raise ValueError(f"Slide {page_id!r} not found in presentation.")
            target = notes_target(slide)
            if target is None or not target.text.strip():
                raise ValueError(f"Slide {page_id!r} has no speaker notes to comment on.")
            if text is None:
                anchor = {"shapeTextAnchor": {"objectId": target.element_id, "textRange": {"type": "ALL"}}}
                location = {"kind": "notes", "slideId": page_id, "slideIndex": slide_index, "elementId": target.element_id}
            else:
                matches = _matches_in(page_id, slide_index, [target], text, None)
                match = _choose(matches, text, occurrence, f" in the speaker notes of slide {page_id!r}")
                anchor, location = _anchor_for_match(match, "notes")
        else:
            matches = find_text(presentation, text, page_id=page_id, element_id=element_id)
            if row is not None:
                matches = [m for m in matches if (m.target.row, m.target.column) == (row, column)]
            scope = ""
            if element_id:
                scope += f" in element {element_id!r}"
            if page_id:
                scope += f" on slide {page_id!r}"
            match = _choose(matches, text, occurrence, scope)
            anchor, location = _anchor_for_match(match, "text")
    elif spans:
        anchor = {
            "tableAnchor": {
                "objectId": element_id,
                "tableRange": {
                    "location": {"rowIndex": row, "columnIndex": column},
                    "rowSpan": row_span or 1,
                    "columnSpan": column_span or 1,
                },
            }
        }
        location = {
            "kind": "table_range", "elementId": element_id,
            "row": row, "column": column, "rowSpan": row_span or 1, "columnSpan": column_span or 1,
        }
    elif row is not None:
        anchor = {
            "tableCellTextAnchor": {
                "objectId": element_id,
                "cellLocation": {"rowIndex": row, "columnIndex": column},
                "textRange": {"type": "ALL"},
            }
        }
        location = {"kind": "table_cells", "elementId": element_id, "cells": [{"row": row, "column": column}]}
    elif element_id:
        anchor = {"objectId": element_id}
        location = {"kind": "element", "elementId": element_id}
    elif page_id:
        anchor = {"objectId": page_id}
        location = {"kind": "slide", "slideId": page_id}
    else:
        raise ValueError(
            "Say where the comment goes: `page_id` (a slide), `element_id`, `text` to "
            "find, or `notes=True` with a `page_id`."
        )

    response = _comment_batch(
        services, presentation_id, [{"insertComment": {"content": content, **anchor}}]
    )
    thread = ((response.get("replies") or [{}])[0].get("insertComment") or {}).get("commentThread")
    if not thread:
        raise RuntimeError("Slides reported success but returned no comment thread.")
    result: dict[str, Any] = {
        "commentId": thread.get("commentId"),
        "status": "open",
        "location": location,
    }
    if thread.get("plainTextQuote"):
        result["quote"] = _clip(thread["plainTextQuote"])
    revision = _revision(response)
    if revision:
        result["revisionId"] = revision
    return result


def reply_to_comment(
    services: Services,
    presentation_id: str,
    comment_id: str,
    content: str = "",
    action: str = "",
) -> dict[str, Any]:
    """Reply to a thread and/or resolve or reopen it (1 API call)."""
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        raise ValueError("`action` must be empty, 'resolve' or 'reopen'.")
    has_content = bool(content and content.strip())
    if not has_content and not action:
        raise ValueError("Give `content`, or an `action` of 'resolve' or 'reopen' (or both).")
    post: dict[str, Any] = {}
    if has_content:
        post["content"] = content
    if action:
        post["commentAction"] = action.upper()
    response = _comment_batch(
        services, presentation_id, [{"addCommentReply": {"commentId": comment_id, "post": post}}]
    )
    reply = ((response.get("replies") or [{}])[0].get("addCommentReply") or {}).get("post") or {}
    result: dict[str, Any] = {"commentId": comment_id, "postId": reply.get("postId")}
    if action:
        result["status"] = "resolved" if action == "resolve" else "open"
    revision = _revision(response)
    if revision:
        result["revisionId"] = revision
    return result


def delete_comment(
    services: Services,
    presentation_id: str,
    comment_id: str,
    post_id: str | None = None,
) -> dict[str, Any]:
    """Delete a whole thread, or one reply when ``post_id`` is given (1 API call)."""
    if post_id:
        request = {"deleteCommentReply": {"commentId": comment_id, "postId": post_id}}
    else:
        request = {"deleteComment": {"commentId": comment_id}}
    response = _comment_batch(services, presentation_id, [request])
    result: dict[str, Any] = {"deleted": "reply" if post_id else "thread", "commentId": comment_id}
    if post_id:
        result["postId"] = post_id
    revision = _revision(response)
    if revision:
        result["revisionId"] = revision
    return result
