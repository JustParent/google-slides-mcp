"""Template / showcase reuse helpers.

The Slides API has **no native cross-presentation slide copy**: ``duplicateObject``
only works within a single presentation, and recreating elements by hand loses
fidelity (placeholder inheritance, master styling, theme colors). The
high-fidelity pattern these helpers implement is:

    1. Drive ``files.copy`` clones the entire showcase deck (masters, layouts,
       theme and color scheme intact).
    2. ``duplicateObject`` copies the wanted example slides *within* that copy.
    3. ``deleteObject`` prunes the originals.
    4. ``replaceAllText`` / text edits fill in content.

The result is functionally identical to hand-editing a copy of the showcase.
"""

from __future__ import annotations

from typing import Any

from .client import Services, execute
from .ids import new_id
from .views import _extract_text, _placeholder, _element_type


def copy_presentation(
    services: Services,
    source_id: str,
    title: str,
    parent_folder_id: str | None = None,
) -> dict[str, Any]:
    """Clone an entire presentation via Drive ``files.copy`` (1 API call).

    This preserves masters, layouts, theme and color scheme — the basis of the
    high-fidelity template workflow.
    """
    body: dict[str, Any] = {"name": title}
    if parent_folder_id:
        body["parents"] = [parent_folder_id]
    result = execute(
        services.drive.files().copy(
            fileId=source_id,
            body=body,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
    )
    new_pid = result["id"]
    return {
        "presentationId": new_pid,
        "title": result.get("name", title),
        "url": result.get("webViewLink")
        or f"https://docs.google.com/presentation/d/{new_pid}/edit",
    }


def catalog_slides(services: Services, presentation_id: str) -> list[dict[str, Any]]:
    """Return a bounded per-slide descriptor to help pick example slides (1 call).

    Each entry summarizes a slide's identity, layout, element makeup, placeholder
    types and a short title snippet — enough to choose which showcase slide to
    reuse for a section, without dumping the full deck.
    """
    presentation = execute(
        services.slides.presentations().get(presentationId=presentation_id)
    )
    catalog: list[dict[str, Any]] = []
    for index, slide in enumerate(presentation.get("slides", [])):
        elements = slide.get("pageElements", [])
        type_counts: dict[str, int] = {}
        placeholders: list[str] = []
        title_snippet: str | None = None
        for element in elements:
            type_counts[_element_type(element)] = (
                type_counts.get(_element_type(element), 0) + 1
            )
            placeholder = _placeholder(element)
            if placeholder and placeholder.get("type"):
                placeholders.append(placeholder["type"])
                if placeholder["type"] in ("TITLE", "CENTERED_TITLE") and not title_snippet:
                    title_snippet = _extract_text(element)
        if title_snippet is None:
            # Fall back to the first text we can find.
            for element in elements:
                title_snippet = _extract_text(element)
                if title_snippet:
                    break
        slide_props = slide.get("slideProperties", {})
        catalog.append(
            {
                "index": index,
                "objectId": slide.get("objectId"),
                "layoutObjectId": slide_props.get("layoutObjectId"),
                "elementCount": len(elements),
                "elementTypes": type_counts,
                "placeholders": placeholders,
                "title": title_snippet,
            }
        )
    return catalog


def duplicate_slide(
    services: Services,
    presentation_id: str,
    page_id: str,
    insertion_index: int | None = None,
) -> dict[str, Any]:
    """Duplicate a slide within a deck with full fidelity (1 batchUpdate call).

    The duplicate is created immediately after the source; if ``insertion_index``
    is given, it is then moved to that position (still one API call).
    """
    new_slide_id = new_id("slide")
    requests: list[dict[str, Any]] = [
        {"duplicateObject": {"objectId": page_id, "objectIds": {page_id: new_slide_id}}}
    ]
    if insertion_index is not None:
        requests.append(
            {
                "updateSlidesPosition": {
                    "slideObjectIds": [new_slide_id],
                    "insertionIndex": insertion_index,
                }
            }
        )
    execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id, body={"requests": requests}
        )
    )
    return {"newSlideId": new_slide_id}


def delete_objects(
    services: Services, presentation_id: str, object_ids: list[str]
) -> dict[str, Any]:
    """Delete pages or page elements in a single batchUpdate call."""
    requests = [{"deleteObject": {"objectId": oid}} for oid in object_ids]
    execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id, body={"requests": requests}
        )
    )
    return {"deleted": object_ids}


def reorder_slides(
    services: Services,
    presentation_id: str,
    slide_ids: list[str],
    insertion_index: int,
) -> dict[str, Any]:
    """Move a block of slides to a new position (1 batchUpdate call)."""
    execute(
        services.slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={
                "requests": [
                    {
                        "updateSlidesPosition": {
                            "slideObjectIds": slide_ids,
                            "insertionIndex": insertion_index,
                        }
                    }
                ]
            },
        )
    )
    return {"reordered": slide_ids, "insertionIndex": insertion_index}
