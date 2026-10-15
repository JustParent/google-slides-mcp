"""FastMCP server exposing low-level Google Slides API tools.

Design principles:
* **Low-level first.** ``batch_update`` exposes the raw Slides write API; the other
  tools are thin, well-documented conveniences over it.
* **Bounded cost.** Every tool makes a fixed, documented number of API calls and
  returns trimmed output, so an LLM driving it has a predictable, finite budget.
* **High-fidelity template reuse.** The template tools copy a whole showcase deck
  then duplicate/prune within it, preserving masters, layouts and theme.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP, Image
from pydantic import Field

from . import comments as comments_mod
from . import render as render_mod
from . import search as search_mod
from . import template as template_mod
from . import textedit
from . import views
from .client import execute, get_services
from .ids import new_id
from .units import to_pt

mcp = FastMCP("google-slides")

# Field masks are required by these requests; warn if the caller forgot one.
_FIELD_MASK_REQUESTS = {"updateShapeProperties", "updateTextStyle", "updatePageProperties"}


# --------------------------------------------------------------------------- #
# Find / import (Drive discovery)
# --------------------------------------------------------------------------- #
@mcp.tool()
def search_presentations(
    name_contains: str | None = None,
    full_text_contains: str | None = None,
    file_type: Annotated[
        str, Field(description="all, google_slides, or pptx")
    ] = "all",
    order_by: Annotated[
        str,
        Field(
            description=(
                "createdTime, modifiedTime, viewedByMeTime, name, or recency — "
                "optionally suffixed with ' desc'"
            )
        ),
    ] = "modifiedTime desc",
    max_results: int = 20,
    owned_by_me: bool | None = None,
    created_after: str | None = None,
    modified_after: str | None = None,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Find existing Google Slides and PowerPoint files in Drive. (1 API call.)

    The discovery entry point: the Slides API has no listing endpoint, so this
    searches Drive across native Slides decks and PowerPoint (``.pptx``/``.ppt``)
    files. With no filters it lists your most recently modified presentations.

    Common recipes:

    * Last deck you created: ``order_by="createdTime desc", owned_by_me=True, max_results=1``.
    * Find the corporate template: ``name_contains="corporate template"``.
    * Decks mentioning a topic: ``full_text_contains="Q3 revenue"`` (note: Drive
      ignores ``order_by`` when ``full_text_contains`` is used).

    Each result includes ``fileType`` and ``directlyEditable``: only
    ``google_slides`` files can be used as ``presentation_id`` with the other
    tools — run ``import_presentation`` on a ``pptx`` result first.

    Args:
        name_contains: Match against the file name (case-insensitive).
        full_text_contains: Match against the file's full text content.
        file_type: ``all`` (default), ``google_slides``, or ``pptx``.
        order_by: Sort key, e.g. ``"modifiedTime desc"`` (default) or
            ``"createdTime desc"``.
        max_results: Page size, 1-100 (default 20).
        owned_by_me: If True, only files you own.
        created_after: RFC3339 timestamp, e.g. ``"2026-01-01T00:00:00"``.
        modified_after: RFC3339 timestamp lower bound on last modification.
        page_token: ``nextPageToken`` from a previous call to fetch more.

    Returns:
        ``{files: [{id, name, fileType, mimeType, createdTime, modifiedTime,
        url, owners, directlyEditable}], nextPageToken?}``.
    """
    return search_mod.search_presentations(
        get_services(),
        name_contains=name_contains,
        full_text_contains=full_text_contains,
        file_type=file_type,
        order_by=order_by,
        max_results=max_results,
        owned_by_me=owned_by_me,
        created_after=created_after,
        modified_after=modified_after,
        page_token=page_token,
    )


@mcp.tool()
def import_presentation(
    file_id: str,
    title: str | None = None,
    parent_folder_id: str | None = None,
) -> dict[str, Any]:
    """Convert a PowerPoint file in Drive to a native Google Slides deck. (1 call.)

    PowerPoint (``.pptx``/``.ppt``) files cannot be edited by the Slides API
    directly. This copies the file with Drive's built-in conversion, leaving the
    original untouched, and returns a ``presentationId`` usable with every other
    tool (including as a ``copy_presentation`` source for the template workflow).
    Conversion is high quality but not guaranteed pixel-perfect — verify with
    ``render_page`` if fidelity matters.

    Args:
        file_id: Drive file ID of the PowerPoint file (from ``search_presentations``).
        title: Title for the converted deck (defaults to the source file's name).
        parent_folder_id: Optional Drive folder to place the converted deck in.
    """
    return search_mod.import_presentation(
        get_services(), file_id, title=title, parent_folder_id=parent_folder_id
    )


# --------------------------------------------------------------------------- #
# Core read
# --------------------------------------------------------------------------- #
@mcp.tool()
def create_presentation(title: str) -> dict[str, Any]:
    """Create a new, empty Google Slides presentation. (1 API call.)

    Args:
        title: The title for the new presentation.

    Returns:
        ``{presentationId, title, url}``.
    """
    services = get_services()
    result = execute(
        services.slides.presentations().create(body={"title": title})
    )
    pid = result["presentationId"]
    return {
        "presentationId": pid,
        "title": result.get("title", title),
        "url": f"https://docs.google.com/presentation/d/{pid}/edit",
    }


@mcp.tool()
def get_presentation(
    presentation_id: str,
    include_masters: bool = False,
    raw: bool = False,
) -> dict[str, Any]:
    """Get a presentation as a bounded, structured overview. (1 API call.)

    By default returns a trimmed summary (slides, elements, positions, short text
    snippets) so large decks stay within a finite output budget. When the deck has
    review comments you can see, the summary also carries ``comments`` (open /
    resolved / orphaned counts) and an ``openComments`` count on each slide; read
    them with ``list_comments``.

    Args:
        presentation_id: The presentation ID.
        include_masters: Also summarize layouts and masters.
        raw: Return the full, untrimmed API response instead of the summary (no
            comments).
    """
    services = get_services()
    options = {} if raw else {"commentsViewMode": comments_mod.DEFAULT_ACCESS}
    presentation = execute(
        services.slides.presentations().get(presentationId=presentation_id, **options)
    )
    if raw:
        return presentation
    return views.summarize_presentation(presentation, include_masters=include_masters)


@mcp.tool()
def get_page(presentation_id: str, page_id: str, raw: bool = False) -> dict[str, Any]:
    """Get a single page (slide) as a bounded summary. (1 API call.)

    The summary has an ``openComments`` count when the slide has open review
    comments you can see; read them with ``list_comments``.

    Args:
        presentation_id: The presentation ID.
        page_id: The page (slide) object ID.
        raw: Return the full, untrimmed API response instead of the summary (no
            comments).
    """
    services = get_services()
    options = {} if raw else {"commentsViewMode": comments_mod.DEFAULT_ACCESS}
    page = execute(
        services.slides.presentations()
        .pages()
        .get(presentationId=presentation_id, pageObjectId=page_id, **options)
    )
    if raw:
        return page
    return views.summarize_page(page)


@mcp.tool()
def list_slides(presentation_id: str) -> list[dict[str, Any]]:
    """List slides with index, id, and layout reference only. (1 API call.)

    The lightest-weight read — use it to find slide IDs before deeper calls. A slide
    with open review comments you can see also gets an ``openComments`` count.
    """
    services = get_services()
    presentation = execute(
        services.slides.presentations().get(
            presentationId=presentation_id, commentsViewMode=comments_mod.DEFAULT_ACCESS
        )
    )
    open_by_slide = (comments_mod.summarize(presentation) or {}).get("bySlide", {})
    out: list[dict[str, Any]] = []
    for index, slide in enumerate(presentation.get("slides", [])):
        slide_props = slide.get("slideProperties", {})
        entry = {
            "index": index,
            "objectId": slide.get("objectId"),
            "layoutObjectId": slide_props.get("layoutObjectId"),
        }
        if open_by_slide.get(slide.get("objectId")):
            entry["openComments"] = open_by_slide[slide["objectId"]]
        out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# Raw write
# --------------------------------------------------------------------------- #
@mcp.tool()
def batch_update(
    presentation_id: str,
    requests: list[dict[str, Any]],
    required_revision_id: str | None = None,
) -> dict[str, Any]:
    """Apply raw Slides ``batchUpdate`` requests atomically. (1 API call.)

    This is the low-level power tool: ``requests`` is a list of Slides API request
    objects (e.g. ``createSlide``, ``insertText``, ``createShape``,
    ``updateTextStyle``, ``updatePageElementTransform``, ``duplicateObject``,
    ``insertComment`` ...).
    See https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/request
    Many edits batched into one call still cost a single write quota unit.

    Note: ``updateShapeProperties``/``updateTextStyle``/``updatePageProperties``
    require a ``fields`` mask; a missing mask is reported as a warning.

    Args:
        presentation_id: The presentation ID.
        requests: A list of single-key Slides request objects.
        required_revision_id: Optional guard for index-based edits: the ``revisionId``
            you read the deck at (from ``get_presentation`` or a previous write). If
            the deck has changed since, nothing is applied and the call fails. Any
            change counts, including someone adding a comment.

    Returns:
        ``{replies, revisionId, commentUpdateState?, warnings?}``: ``replies`` is the
        API response array; ``revisionId`` is the deck's revision after the write
        (pass it as the next ``required_revision_id``); ``commentUpdateState`` appears
        when the batch contained comment requests, with a warning if they weren't saved.
    """
    if not isinstance(requests, list) or not requests:
        raise ValueError("`requests` must be a non-empty list of request objects.")

    warnings: list[str] = []
    for i, request in enumerate(requests):
        if not isinstance(request, dict) or len(request) != 1:
            raise ValueError(
                f"Request {i} must be a dict with exactly one request-type key."
            )
        (kind, body), = request.items()
        if kind in _FIELD_MASK_REQUESTS and not (
            isinstance(body, dict) and body.get("fields")
        ):
            warnings.append(
                f"Request {i} ({kind}) is missing a `fields` mask, which the API "
                "requires; the call may fail or behave unexpectedly."
            )

    body: dict[str, Any] = {"requests": requests}
    if required_revision_id:
        body["writeControl"] = {"requiredRevisionId": required_revision_id}

    services = get_services()
    response = execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id, body=body
        )
    )
    return comments_mod.batch_summary(response, warnings)


# --------------------------------------------------------------------------- #
# Element / transform / bounding box
# --------------------------------------------------------------------------- #
def _find_element(presentation: dict[str, Any], element_id: str) -> dict[str, Any] | None:
    for page_key in ("slides", "layouts", "masters"):
        for page in presentation.get(page_key, []):
            found = _search_elements(page.get("pageElements", []), element_id)
            if found:
                return found
    return None


def _search_elements(
    elements: list[dict[str, Any]], element_id: str
) -> dict[str, Any] | None:
    for element in elements:
        if element.get("objectId") == element_id:
            return element
        group = element.get("elementGroup")
        if group:
            found = _search_elements(group.get("children", []), element_id)
            if found:
                return found
    return None


@mcp.tool()
def set_element_box(
    presentation_id: str,
    element_id: str,
    x_pt: float,
    y_pt: float,
    width_pt: float | None = None,
    height_pt: float | None = None,
) -> dict[str, Any]:
    """Set an element's position (and optionally size) in points. (Up to 2 calls.)

    A convenience over ``updatePageElementTransform`` that lets you place a
    bounding box by its top-left corner in points without computing an affine
    matrix. Reads the element's current transform once (to preserve scale/shear
    when size is omitted), then applies one absolute transform update.

    Args:
        presentation_id: The presentation ID.
        element_id: The page element to move/resize.
        x_pt: Left edge (top-left X) in points.
        y_pt: Top edge (top-left Y) in points.
        width_pt: Desired visual width in points (optional; preserves current if omitted).
        height_pt: Desired visual height in points (optional; preserves current if omitted).
    """
    services = get_services()
    presentation = execute(
        services.slides.presentations().get(presentationId=presentation_id)
    )
    element = _find_element(presentation, element_id)
    if element is None:
        raise ValueError(f"Element {element_id!r} not found in presentation.")

    transform = element.get("transform", {})
    size = element.get("size", {})
    scale_x = transform.get("scaleX", 1.0)
    scale_y = transform.get("scaleY", 1.0)
    shear_x = transform.get("shearX", 0.0)
    shear_y = transform.get("shearY", 0.0)

    if width_pt is not None:
        intrinsic_w = to_pt(size.get("width"))
        if not intrinsic_w:
            raise ValueError(
                f"Element {element_id!r} has no intrinsic width; cannot set width_pt."
            )
        scale_x = width_pt / intrinsic_w
    if height_pt is not None:
        intrinsic_h = to_pt(size.get("height"))
        if not intrinsic_h:
            raise ValueError(
                f"Element {element_id!r} has no intrinsic height; cannot set height_pt."
            )
        scale_y = height_pt / intrinsic_h

    new_transform = {
        "scaleX": scale_x,
        "scaleY": scale_y,
        "shearX": shear_x,
        "shearY": shear_y,
        "translateX": x_pt,
        "translateY": y_pt,
        "unit": "PT",
    }
    execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={
                "requests": [
                    {
                        "updatePageElementTransform": {
                            "objectId": element_id,
                            "transform": new_transform,
                            "applyMode": "ABSOLUTE",
                        }
                    }
                ]
            },
        )
    )
    return {"objectId": element_id, "transform": new_transform}


@mcp.tool()
def update_transform(
    presentation_id: str,
    element_id: str,
    transform: dict[str, Any],
    mode: Annotated[str, Field(description="ABSOLUTE or RELATIVE")] = "ABSOLUTE",
) -> dict[str, Any]:
    """Apply a raw AffineTransform to an element. (1 API call.)

    Args:
        presentation_id: The presentation ID.
        element_id: The page element ID.
        transform: An AffineTransform dict (scaleX, scaleY, shearX, shearY,
            translateX, translateY, unit). ``unit`` may be ``EMU`` or ``PT``.
        mode: ``ABSOLUTE`` (replace) or ``RELATIVE`` (multiply with existing).
    """
    services = get_services()
    execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={
                "requests": [
                    {
                        "updatePageElementTransform": {
                            "objectId": element_id,
                            "transform": transform,
                            "applyMode": mode,
                        }
                    }
                ]
            },
        )
    )
    return {"objectId": element_id, "applyMode": mode}


@mcp.tool()
def set_z_order(
    presentation_id: str,
    element_ids: list[str],
    operation: Annotated[
        str,
        Field(description="BRING_TO_FRONT, SEND_TO_BACK, BRING_FORWARD, or SEND_BACKWARD"),
    ],
) -> dict[str, Any]:
    """Change the front/back stacking order of elements. (1 API call.)"""
    services = get_services()
    execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={
                "requests": [
                    {
                        "updatePageElementsZOrder": {
                            "pageElementObjectIds": element_ids,
                            "operation": operation,
                        }
                    }
                ]
            },
        )
    )
    return {"elementIds": element_ids, "operation": operation}


@mcp.tool()
def group_elements(
    presentation_id: str, element_ids: list[str]
) -> dict[str, Any]:
    """Group two or more elements into a single group. (1 API call.)"""
    group_id = new_id("group")
    services = get_services()
    execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={
                "requests": [
                    {
                        "groupObjects": {
                            "groupObjectId": group_id,
                            "childrenObjectIds": element_ids,
                        }
                    }
                ]
            },
        )
    )
    return {"groupId": group_id, "children": element_ids}


@mcp.tool()
def ungroup_elements(
    presentation_id: str, group_ids: list[str]
) -> dict[str, Any]:
    """Ungroup one or more groups back into individual elements. (1 API call.)"""
    services = get_services()
    execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": [{"ungroupObjects": {"objectIds": group_ids}}]},
        )
    )
    return {"ungrouped": group_ids}


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #
@mcp.tool()
def replace_all_text(
    presentation_id: str,
    mappings: dict[str, str],
    page_ids: list[str] | None = None,
    match_case: bool = True,
) -> dict[str, Any]:
    """Replace all occurrences of each placeholder string. (1 API call.)

    The primary content-fill tool for the template workflow: map showcase
    placeholder text to real content. All replacements run in one batch.

    Args:
        presentation_id: The presentation ID.
        mappings: ``{find: replace}`` pairs.
        page_ids: Restrict to these pages (optional; default = whole deck).
        match_case: Case-sensitive matching.
    """
    requests: list[dict[str, Any]] = []
    for find, replacement in mappings.items():
        request: dict[str, Any] = {
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": match_case},
                "replaceText": replacement,
            }
        }
        if page_ids:
            request["replaceAllText"]["pageObjectIds"] = page_ids
        requests.append(request)

    services = get_services()
    response = execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id, body={"requests": requests}
        )
    )
    occurrences = sum(
        reply.get("replaceAllText", {}).get("occurrencesChanged", 0)
        for reply in response.get("replies", [])
    )
    return {"occurrencesChanged": occurrences}


@mcp.tool()
def insert_text(
    presentation_id: str,
    element_id: str,
    text: str,
    index: int = 0,
) -> dict[str, Any]:
    """Insert text into a shape or table cell at a character index. (1 API call.)"""
    services = get_services()
    execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "objectId": element_id,
                            "text": text,
                            "insertionIndex": index,
                        }
                    }
                ]
            },
        )
    )
    return {"objectId": element_id, "inserted": len(text)}


@mcp.tool()
def set_element_text(
    presentation_id: str,
    element_id: str,
    text: str,
    row: int | None = None,
    column: int | None = None,
) -> dict[str, Any]:
    """Make a shape's (or table cell's) text exactly ``text``. (2 API calls.)

    Reads the deck, then changes only the part of the text that differs, so review
    comments on the unchanged words stay attached and a comment on a phrase you
    rewrite usually stays on the new phrase (deleting everything and retyping would
    detach them). Safe to call on an empty element. This is the ergonomic way to fill
    a freshly duplicated slide when the showcase example uses real text rather than
    ``{{placeholder}}`` tokens.

    The edit is written against the revision it was computed from and recomputed
    once if the deck changed in between (up to 4 calls).

    Args:
        presentation_id: The presentation ID.
        element_id: The shape's object ID, or a table's object ID together with
            ``row`` and ``column``.
        text: The new full text content.
        row: Row of the table cell to set (0-based); tables only.
        column: Column of the table cell to set (0-based); tables only.
    """
    return textedit.set_element_text(
        get_services(), presentation_id, element_id, text, row=row, column=column
    )


# --------------------------------------------------------------------------- #
# Review comments
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_comments(
    presentation_id: str,
    page_id: str | None = None,
    status: Annotated[str, Field(description="open, resolved, or all")] = "open",
) -> dict[str, Any]:
    """List review comment threads and exactly where each one sits. (1 API call.)

    Use it to find feedback to act on. Each thread has its ``commentId``, author,
    ``content``, ``replies`` and the ``quote`` it was made on, plus ``locations``,
    each with a ``kind``:

    * ``slide`` / ``element``: the whole slide, or a whole shape, image or table;
    * ``text``: a range in a shape (``elementId``, UTF-16 ``start``/``end`` and the
      ``text`` it covers);
    * ``table_cells``: ``cells`` with ``row``/``column`` (0-based) and text range;
    * ``notes``: the slide's speaker notes.

    ``slideId`` and ``slideIndex`` say which slide. A thread whose commented content
    was deleted has no location and is flagged ``orphaned`` (it is reported only in
    whole-deck listings). Reading comments needs comment access to the deck.

    Args:
        presentation_id: The presentation ID.
        page_id: Only threads anchored on this slide.
        status: ``open`` (default), ``resolved`` or ``all``.

    Returns:
        ``{presentationId, revisionId, counts: {open, resolved, orphaned}, threads}``.
        ``counts`` cover the whole scope whatever ``status`` is. At most 100 threads
        are returned; ``threadsTruncated`` says when there were more.
    """
    return comments_mod.list_comments(
        get_services(), presentation_id, page_id=page_id, status=status
    )


@mcp.tool()
def add_comment(
    presentation_id: str,
    content: str,
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
    """Start a comment thread on a slide, element, quoted text, table cells or notes. (1-2 API calls.)

    Slides has no suggestion mode, so a comment is the way to propose a change, ask a
    question or flag a problem without editing the slide. Say where it goes with one
    of these combinations:

    * ``page_id`` alone: the whole slide.
    * ``element_id`` alone: the whole shape, image or table.
    * ``text``: the quoted words, found in any shape or table cell (exact,
      case-sensitive); add ``page_id`` and/or ``element_id`` to narrow the search. If
      the quote appears more than once the call fails and lists the candidates: pass
      ``occurrence`` (1-based, in reading order) and/or ``element_id`` to choose.
    * ``element_id`` (a table) with ``row`` and ``column``: one cell; add ``text`` to
      quote words inside it, or ``row_span`` / ``column_span`` for a block of cells.
    * ``notes=True`` with ``page_id``: the slide's speaker notes; add ``text`` to
      quote words inside them.

    A quote or the notes are located with one extra read (2 calls); the other forms
    are a single write.

    Args:
        presentation_id: The presentation ID.
        content: The comment text (plain text).
        page_id: The slide's object ID.
        element_id: The shape or table's object ID.
        text: Words to anchor the comment to.
        occurrence: Which occurrence of ``text`` (1-based) when it appears more than once.
        row: Table cell row (0-based).
        column: Table cell column (0-based).
        row_span: Rows in a block of table cells (default 1).
        column_span: Columns in a block of table cells (default 1).
        notes: Anchor to the speaker notes of ``page_id``.

    Returns:
        ``{commentId, status, location, quote?, revisionId}``.
    """
    return comments_mod.add_comment(
        get_services(),
        presentation_id,
        content,
        page_id=page_id,
        element_id=element_id,
        text=text,
        occurrence=occurrence,
        row=row,
        column=column,
        row_span=row_span,
        column_span=column_span,
        notes=notes,
    )


@mcp.tool()
def reply_to_comment(
    presentation_id: str,
    comment_id: str,
    content: str = "",
    action: Annotated[
        str, Field(description="resolve, reopen, or empty for a plain reply")
    ] = "",
) -> dict[str, Any]:
    """Reply to a comment thread, and/or resolve or reopen it. (1 API call.)

    After acting on a piece of feedback, reply with what you changed and resolve the
    thread. ``content`` may be empty when ``action`` is given.

    Args:
        presentation_id: The presentation ID.
        comment_id: The thread's ``commentId`` (from ``list_comments``).
        content: The reply text (plain text).
        action: ``resolve``, ``reopen``, or empty for a plain reply.

    Returns:
        ``{commentId, postId, status?, revisionId}``; ``status`` is set when ``action``
        was given.
    """
    return comments_mod.reply_to_comment(
        get_services(), presentation_id, comment_id, content=content, action=action
    )


@mcp.tool()
def delete_comment(
    presentation_id: str,
    comment_id: str,
    post_id: str | None = None,
) -> dict[str, Any]:
    """Delete a comment thread, or just one reply of it. (1 API call.)

    Without ``post_id`` the whole thread is deleted; with it, only that reply (its
    ``postId`` is in ``list_comments``). You can delete only what you wrote, and a
    reply that resolved or reopened the thread can't be deleted: to undo a resolution
    use ``reply_to_comment`` with ``action="reopen"``.

    Args:
        presentation_id: The presentation ID.
        comment_id: The thread's ``commentId``.
        post_id: A reply's ``postId``, to delete only that reply.
    """
    return comments_mod.delete_comment(
        get_services(), presentation_id, comment_id, post_id=post_id
    )


# --------------------------------------------------------------------------- #
# Template / copy (showcase workflow)
# --------------------------------------------------------------------------- #
@mcp.tool()
def copy_presentation(
    source_id: str,
    title: str,
    parent_folder_id: str | None = None,
) -> dict[str, Any]:
    """Clone an entire deck (Drive files.copy), preserving full styling. (1 call.)

    The starting point of the high-fidelity template workflow: copies the showcase
    deck with all masters, layouts, theme and color scheme intact, so the copy can
    be edited exactly as if you were editing the showcase itself.

    Args:
        source_id: The presentation ID of the template/showcase to copy.
        title: Title for the new copy.
        parent_folder_id: Optional Drive folder to place the copy in.
    """
    return template_mod.copy_presentation(
        get_services(), source_id, title, parent_folder_id
    )


@mcp.tool()
def catalog_slides(presentation_id: str) -> list[dict[str, Any]]:
    """Summarize each slide (layout, element makeup, placeholders, title). (1 call.)

    Use after ``copy_presentation`` to choose which showcase example slide to reuse
    for each section of the new deck.
    """
    return template_mod.catalog_slides(get_services(), presentation_id)


@mcp.tool()
def duplicate_slide(
    presentation_id: str,
    page_id: str,
    insertion_index: int | None = None,
) -> dict[str, Any]:
    """Duplicate a slide within a deck with full fidelity. (1 API call.)

    Copies all of the slide's elements and inherited styling. Use it to instantiate
    a showcase example slide for a new section, then fill it with ``replace_all_text``.

    Args:
        presentation_id: The presentation ID.
        page_id: The slide to duplicate.
        insertion_index: Where to place the duplicate (optional).
    """
    return template_mod.duplicate_slide(
        get_services(), presentation_id, page_id, insertion_index
    )


@mcp.tool()
def delete_objects(
    presentation_id: str, object_ids: list[str]
) -> dict[str, Any]:
    """Delete slides or page elements by ID. (1 API call.)

    Use to prune the original showcase example slides after duplicating the ones
    you want.
    """
    return template_mod.delete_objects(get_services(), presentation_id, object_ids)


@mcp.tool()
def reorder_slides(
    presentation_id: str,
    slide_ids: list[str],
    insertion_index: int,
) -> dict[str, Any]:
    """Move a block of slides to a new position. (1 API call.)"""
    return template_mod.reorder_slides(
        get_services(), presentation_id, slide_ids, insertion_index
    )


# --------------------------------------------------------------------------- #
# Iteration / palette
# --------------------------------------------------------------------------- #
@mcp.tool()
def park_slides(
    presentation_id: str, slide_ids: list[str]
) -> dict[str, Any]:
    """Hide slides (mark skipped) so they stay as a clone source. (1 API call.)

    Keeps the showcase example/original slides in the deck as a reusable "palette"
    while hiding them from presentation mode. Duplicate from them across as many
    turns as you like, then ``prune_parked_slides`` at the very end.
    """
    return template_mod.park_slides(get_services(), presentation_id, slide_ids)


@mcp.tool()
def unpark_slides(
    presentation_id: str, slide_ids: list[str]
) -> dict[str, Any]:
    """Unhide slides previously parked (mark not skipped). (1 API call.)"""
    return template_mod.unpark_slides(get_services(), presentation_id, slide_ids)


@mcp.tool()
def prune_parked_slides(presentation_id: str) -> dict[str, Any]:
    """Delete every parked (hidden/skipped) slide — final cleanup. (<=2 API calls.)

    Removes ALL slides currently marked skipped, so use it deliberately as the last
    step once the deck is assembled.
    """
    return template_mod.prune_parked_slides(get_services(), presentation_id)


# --------------------------------------------------------------------------- #
# Rendering / verify
# --------------------------------------------------------------------------- #
@mcp.tool()
def render_page(
    presentation_id: str,
    page_id: str,
    size: Annotated[str, Field(description="LARGE, MEDIUM, or SMALL")] = "MEDIUM",
) -> Image:
    """Render a slide to a PNG image for visual verification. (1 expensive call.)

    Returns the server-side rendered pixels of the slide — the same image a viewer
    sees — so you can confirm output looks correct. Rendering is per-page by design
    (the thumbnail endpoint is an expensive quota operation).

    Args:
        presentation_id: The presentation ID.
        page_id: The slide object ID.
        size: LARGE (~1600px), MEDIUM (~800px), or SMALL (~200px) wide.
    """
    result = render_mod.render(get_services(), presentation_id, page_id, size=size)
    return Image(data=result["png"], format="png")


@mcp.tool()
def diff_pages(
    presentation_id: str,
    page_a: str,
    page_b: str,
    size: Annotated[str, Field(description="LARGE, MEDIUM, or SMALL")] = "MEDIUM",
) -> list[Any]:
    """Render two slides and report their pixel difference. (2 expensive calls.)

    Renders both pages, computes a normalized mismatch ratio in [0, 1] (0 = pixel
    identical), and returns a visual diff image highlighting changed regions. Useful
    to verify a duplicated/edited slide matches the showcase original.

    Returns a text summary followed by the diff PNG.
    """
    services = get_services()
    a = render_mod.render(services, presentation_id, page_a, size=size)
    b = render_mod.render(services, presentation_id, page_b, size=size)
    comparison = render_mod.diff(a["png"], b["png"])
    summary = (
        f"mismatch={comparison['mismatch']} identical={comparison['identical']} "
        f"(0.0 = pixel-perfect match)"
    )
    return [summary, Image(data=comparison["diff_png"], format="png")]


def main() -> None:
    """Console entry point: ``google-slides-mcp`` (stdio transport).

    Resolve credentials before starting the stdio loop. A cached token returns
    immediately; an unauthenticated first launch runs the interactive consent flow
    (unless ``GOOGLE_SLIDES_NO_BROWSER_AUTH`` is set). Failures are logged to stderr
    and the server still starts, so tool calls surface a clear auth error.
    """
    from .auth import ensure_credentials

    ensure_credentials()
    mcp.run()


if __name__ == "__main__":
    main()
