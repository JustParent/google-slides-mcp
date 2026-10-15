"""Builders shaped like real Slides API responses, and a fake service that records calls.

The structures mirror what the live API returned when the comments support was
verified (textElements with a paragraph marker and a text run, table cells whose
``location`` omits zeros, notes pages with ``notesProperties``, comment anchors on
slides and on notes pages). No network and no credentials.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httplib2
from googleapiclient.errors import HttpError

from google_slides_mcp.client import Services
from google_slides_mcp.comments import utf16_len


# --------------------------------------------------------------------------- #
# Deck builders
# --------------------------------------------------------------------------- #
def text_content(text: str) -> dict[str, Any]:
    full = text + "\n"
    end = utf16_len(full)
    return {
        "textElements": [
            {"endIndex": end, "paragraphMarker": {"style": {}}},
            {"endIndex": end, "textRun": {"content": full, "style": {}}},
        ]
    }


def shape(object_id: str, text: str | None = None) -> dict[str, Any]:
    element: dict[str, Any] = {"objectId": object_id, "shape": {"shapeType": "TEXT_BOX"}}
    if text is not None:
        element["shape"]["text"] = text_content(text)
    return element


def table(object_id: str, rows: list[list[str]]) -> dict[str, Any]:
    table_rows = []
    for r, row in enumerate(rows):
        cells = []
        for c, text in enumerate(row):
            location: dict[str, int] = {}  # the API omits zero values
            if r:
                location["rowIndex"] = r
            if c:
                location["columnIndex"] = c
            cells.append({"location": location, "text": text_content(text)})
        table_rows.append({"tableCells": cells})
    return {
        "objectId": object_id,
        "table": {"rows": len(rows), "columns": len(rows[0]), "tableRows": table_rows},
    }


def group(object_id: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"objectId": object_id, "elementGroup": {"children": children}}


def anchor(anchor_id: str, *object_anchors: dict[str, Any]) -> dict[str, Any]:
    return {"anchorId": anchor_id, "objectAnchors": list(object_anchors)}


def text_anchor(object_id: str, start: int, end: int) -> dict[str, Any]:
    return {"objectId": object_id, "shapeTextAnchors": {"ranges": [{"startIndex": start, "endIndex": end}]}}


def cell_anchor(object_id: str, *cells: tuple[int, int, int, int]) -> dict[str, Any]:
    """Table cell anchors; each cell is (row, column, start, end). Zeros are omitted."""
    cell_ranges = []
    for row, column, start, end in cells:
        location: dict[str, int] = {}
        if row:
            location["rowIndex"] = row
        if column:
            location["columnIndex"] = column
        cell_ranges.append(
            {"cellLocation": location, "ranges": [{"startIndex": start, "endIndex": end}]}
        )
    return {"objectId": object_id, "tableCellAnchors": {"cellRanges": cell_ranges}}


def slide(
    object_id: str,
    elements: list[dict[str, Any]] | None = None,
    *,
    notes: str | None = None,
    anchors: list[dict[str, Any]] | None = None,
    notes_anchors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "objectId": object_id,
        "pageElements": elements or [],
        "slideProperties": {"layoutObjectId": "layout1"},
    }
    if anchors:
        result["commentAnchors"] = anchors
    if notes is not None:
        notes_page: dict[str, Any] = {
            "objectId": f"{object_id}:notes",
            "pageType": "NOTES",
            "notesProperties": {"speakerNotesObjectId": f"{object_id}_notes"},
            "pageElements": [
                {"objectId": f"{object_id}_image", "shape": {"placeholder": {"type": "SLIDE_IMAGE"}}},
                shape(f"{object_id}_notes", notes),
            ],
        }
        if notes_anchors:
            notes_page["commentAnchors"] = notes_anchors
        result["slideProperties"]["notesPage"] = notes_page
    return result


def post(
    post_id: str,
    content: str | None = None,
    *,
    author: str = "Reviewer A",
    created: str = "2026-10-01T10:00:00Z",
    me: bool = False,
    action: str | None = None,
    deleted: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "postId": post_id,
        "author": {"displayName": author, **({"me": True} if me else {})},
        "createTime": created,
        "commentAction": action or "NO_COMMENT_ACTION_CHANGE",
    }
    if content is not None:
        result["content"] = content
    if deleted:
        result = {"postId": post_id, "deleted": True}
    return result


def thread(
    comment_id: str,
    anchor_id: str,
    content: str,
    *,
    status: str = "OPEN",
    quote: str = "",
    replies: list[dict[str, Any]] | None = None,
    **post_options: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "commentId": comment_id,
        "anchorId": anchor_id,
        "status": status,
        "headPost": post(comment_id, content, **post_options),
    }
    if quote:
        result["plainTextQuote"] = quote
    if replies:
        result["replies"] = replies
    return result


def deck(
    slides: list[dict[str, Any]],
    comments: list[dict[str, Any]] | None = None,
    revision: str = "rev1",
) -> dict[str, Any]:
    result: dict[str, Any] = {"presentationId": "pid", "revisionId": revision, "slides": slides}
    if comments is not None:
        result["comments"] = comments
    return result


# --------------------------------------------------------------------------- #
# Sample decks shared by the tests
# --------------------------------------------------------------------------- #
BODY = "Revenue grew 12% year on year"  # "12%" sits at 13..16
NOTES = "Remember the churn caveat."


def lookup_deck():
    return deck(
        [
            slide(
                "s1",
                [
                    shape("title", "Quarterly review"),
                    shape("body", BODY),
                    table("tbl", [["Metric", "Value"], ["Revenue", "12% up"]]),
                    group("grp", [shape("inner", "inside the group 12%")]),
                    shape("emoji", "\U0001F600 grew 12% today"),
                ],
                notes=NOTES + " Churn is 3%.",
            ),
            slide("s2", [shape("again", "Growth 12% and 12% again")]),
        ]
    )


def commented_deck():
    s1 = slide(
        "s1",
        [shape("title", "Quarterly review"), shape("body", BODY), table("tbl", [["Metric", "Value"], ["Revenue", "12% up"]])],
        notes=NOTES,
        anchors=[
            anchor("a_slide", {"objectId": "s1"}),
            anchor("a_elem", {"objectId": "body"}),
            anchor("a_text", text_anchor("body", 13, 16)),
            anchor("a_cells", cell_anchor("tbl", (0, 0, 0, 6), (1, 1, 0, 3))),
        ],
        notes_anchors=[anchor("a_notes", text_anchor("s1_notes", 0, 26))],
    )
    # A duplicated slide: the same text anchor now also sits on slide 2.
    s2 = slide("s2", [shape("other", "Second slide")], anchors=[anchor("a_text", text_anchor("other", 0, 6))])
    threads = [
        thread("c_orphan", "a_gone", "About the old chart", quote="old chart", created="2026-10-01T10:06:00Z"),
        thread("c_done", "a_slide", "Typo", status="RESOLVED", quote="Quarterly review", created="2026-10-01T10:05:00Z"),
        thread("c_notes", "a_notes", "Fix wording", quote=NOTES, created="2026-10-01T10:04:00Z"),
        thread("c_cells", "a_cells", "Units?", quote="Metric", created="2026-10-01T10:03:00Z"),
        thread(
            "c_text", "a_text", "Is it 12 or 14?", quote="12%", created="2026-10-01T10:02:00Z",
            replies=[
                post("p1", "Checked: 12.", author="Reviewer B", created="2026-10-01T11:00:00Z"),
                post("p2", deleted=True),
                post("p3", None, author="Reviewer B", created="2026-10-01T11:05:00Z", action="REOPEN", me=True),
            ],
        ),
        thread("c_elem", "a_elem", "Check this claim", quote=BODY, created="2026-10-01T10:01:00Z"),
        thread("c_slide", "a_slide", "Retitle this slide", quote="Quarterly review", created="2026-10-01T10:00:00Z", me=True),
    ]
    return deck([s1, s2], threads)


# --------------------------------------------------------------------------- #
# Fake service
# --------------------------------------------------------------------------- #
class _Request:
    def __init__(self, produce):
        self._produce = produce

    def execute(self):
        return self._produce()


class FakeSlides:
    """Stands in for ``slides`` from ``googleapiclient``; records every call it gets."""

    def __init__(self, deck: Any = None, batch_responses: list[Any] | None = None):
        # A list serves successive reads in turn (the last one repeats), so a test can
        # have the deck change between two reads.
        self.decks: list[dict[str, Any]] = deck if isinstance(deck, list) else [deck or {}]
        self.page: dict[str, Any] = {}
        self.batch_responses = list(batch_responses or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # presentations().get / .batchUpdate / .pages().get
    def presentations(self):
        return self

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))

        def produce():
            current = self.decks.pop(0) if len(self.decks) > 1 else self.decks[0]
            if isinstance(current, Exception):
                raise current
            return copy.deepcopy(current)

        return _Request(produce)

    def batchUpdate(self, **kwargs):
        self.calls.append(("batchUpdate", kwargs))

        def produce():
            if not self.batch_responses:
                return {"replies": [{}]}
            item = self.batch_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return copy.deepcopy(item)

        return _Request(produce)

    def pages(self):
        return _Pages(self)

    # helpers for assertions
    def of(self, method: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == method]


class _Pages:
    def __init__(self, owner: FakeSlides):
        self.owner = owner

    def get(self, **kwargs):
        self.owner.calls.append(("pages.get", kwargs))
        return _Request(lambda: copy.deepcopy(self.owner.page))


def fake_services(
    deck: Any = None, batch_responses: list[Any] | None = None
) -> tuple[Services, FakeSlides]:
    slides = FakeSlides(deck, batch_responses)
    return Services(slides=slides, drive=None), slides


def http_error(status: int, message: str) -> HttpError:
    content = json.dumps({"error": {"code": status, "message": message}}).encode()
    return HttpError(httplib2.Response({"status": status}), content)


def inserted(comment_id: str = "c1", quote: str = "", revision: str = "rev2") -> dict[str, Any]:
    """A batchUpdate response to a successful ``insertComment``."""
    created: dict[str, Any] = {"commentId": comment_id, "status": "OPEN", "headPost": {"postId": comment_id}}
    if quote:
        created["plainTextQuote"] = quote
    return {
        "replies": [{"insertComment": {"commentThread": created}}],
        "commentUpdateState": "ALL_SAVED",
        "writeControl": {"requiredRevisionId": revision},
    }
