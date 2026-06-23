"""Bounded, structured views over verbose Slides API responses.

A full ``presentations.get`` can be enormous (every inherited style on every run
of text). To keep tool output finite, these helpers trim responses to the
structure an LLM actually needs to reason about a deck: slide/element identity,
type, position, and short text snippets. Callers that genuinely need everything
can pass ``raw=True`` at the tool layer to bypass trimming.
"""

from __future__ import annotations

from typing import Any

from .units import bounding_box

# Bounds that keep output finite regardless of deck size.
MAX_TEXT_SNIPPET = 280
MAX_ELEMENTS_PER_PAGE = 100


def _element_type(element: dict[str, Any]) -> str:
    for key in (
        "shape",
        "table",
        "image",
        "line",
        "video",
        "wordArt",
        "sheetsChart",
        "elementGroup",
    ):
        if key in element:
            return key
    return "unknown"


def _extract_text(element: dict[str, Any]) -> str | None:
    """Pull a short plain-text snippet from a shape/table element."""
    pieces: list[str] = []

    def _from_text(text: dict[str, Any]) -> None:
        for run in text.get("textElements", []):
            content = run.get("textRun", {}).get("content")
            if content:
                pieces.append(content)

    shape = element.get("shape")
    if shape and "text" in shape:
        _from_text(shape["text"])

    table = element.get("table")
    if table:
        for row in table.get("tableRows", []):
            for cell in row.get("tableCells", []):
                if "text" in cell:
                    _from_text(cell["text"])

    if not pieces:
        return None
    joined = "".join(pieces).strip()
    if not joined:
        return None
    if len(joined) > MAX_TEXT_SNIPPET:
        return joined[:MAX_TEXT_SNIPPET] + "…"
    return joined


def _placeholder(element: dict[str, Any]) -> dict[str, Any] | None:
    placeholder = element.get("shape", {}).get("placeholder")
    if not placeholder:
        return None
    out = {"type": placeholder.get("type")}
    if "index" in placeholder:
        out["index"] = placeholder["index"]
    return out


def summarize_element(element: dict[str, Any]) -> dict[str, Any]:
    """Trim a single page element to its essential, bounded fields."""
    summary: dict[str, Any] = {
        "objectId": element.get("objectId"),
        "type": _element_type(element),
    }
    box = bounding_box(element.get("size"), element.get("transform"))
    if box:
        summary["box_pt"] = box
    placeholder = _placeholder(element)
    if placeholder:
        summary["placeholder"] = placeholder
    text = _extract_text(element)
    if text:
        summary["text"] = text
    group = element.get("elementGroup")
    if group:
        children = group.get("children", [])
        summary["children"] = [c.get("objectId") for c in children]
    return summary


def summarize_page(page: dict[str, Any]) -> dict[str, Any]:
    """Trim a page (slide) to identity, layout reference, and element summaries."""
    elements = page.get("pageElements", [])
    truncated = len(elements) > MAX_ELEMENTS_PER_PAGE
    shown = elements[:MAX_ELEMENTS_PER_PAGE]

    slide_props = page.get("slideProperties", {})
    summary: dict[str, Any] = {
        "objectId": page.get("objectId"),
        "pageType": page.get("pageType", "SLIDE"),
        "elementCount": len(elements),
        "elements": [summarize_element(e) for e in shown],
    }
    if slide_props:
        summary["layoutObjectId"] = slide_props.get("layoutObjectId")
        summary["masterObjectId"] = slide_props.get("masterObjectId")
        # Only surface when parked/hidden, to keep output lean.
        if slide_props.get("isSkipped"):
            summary["isSkipped"] = True
    if truncated:
        summary["elementsTruncated"] = True
    return summary


def summarize_presentation(
    presentation: dict[str, Any], include_masters: bool = False
) -> dict[str, Any]:
    """Trim a full presentation into a bounded structured overview.

    Args:
        presentation: The raw ``presentations.get`` response.
        include_masters: When True, also summarize masters and layouts.
    """
    page_size = presentation.get("pageSize", {})
    summary: dict[str, Any] = {
        "presentationId": presentation.get("presentationId"),
        "title": presentation.get("title"),
        "revisionId": presentation.get("revisionId"),
        "pageSize_pt": {
            "width": _dim_pt(page_size.get("width")),
            "height": _dim_pt(page_size.get("height")),
        },
        "slideCount": len(presentation.get("slides", [])),
        "slides": [summarize_page(p) for p in presentation.get("slides", [])],
    }
    if include_masters:
        summary["layouts"] = [
            summarize_page(p) for p in presentation.get("layouts", [])
        ]
        summary["masters"] = [
            summarize_page(p) for p in presentation.get("masters", [])
        ]
    return summary


def _dim_pt(dimension: dict[str, Any] | None) -> float | None:
    from .units import to_pt

    value = to_pt(dimension)
    return round(value, 3) if value is not None else None
