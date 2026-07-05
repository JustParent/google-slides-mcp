"""Drive-based discovery and import of existing presentations.

The Slides API itself has no listing/search endpoint, so finding "the last deck
I created" or "our corporate template" goes through Drive ``files.list``. These
helpers build bounded Drive queries over the presentation mime types (native
Google Slides plus PowerPoint ``.pptx``/``.ppt``) and return trimmed results.

PowerPoint files stored in Drive cannot be edited by the Slides API directly;
:func:`import_presentation` converts one into a native Google Slides deck via
Drive ``files.copy`` with a target mime type, after which every other tool in
this server works on it.
"""

from __future__ import annotations

from typing import Any

from .client import Services, execute

GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PPT_MIME = "application/vnd.ms-powerpoint"

# file_type filter -> Drive mime types it covers.
FILE_TYPE_MIMES: dict[str, list[str]] = {
    "google_slides": [GOOGLE_SLIDES_MIME],
    "pptx": [PPTX_MIME, PPT_MIME],
    "all": [GOOGLE_SLIDES_MIME, PPTX_MIME, PPT_MIME],
}

# Drive `orderBy` keys this server accepts (each may be suffixed with " desc").
ORDER_BY_FIELDS = {"createdTime", "modifiedTime", "viewedByMeTime", "name", "recency"}

_MAX_RESULTS_CAP = 100

_LIST_FIELDS = (
    "nextPageToken, files(id, name, mimeType, createdTime, modifiedTime, "
    "webViewLink, owners(displayName, emailAddress), size)"
)


def escape_query_value(value: str) -> str:
    """Escape a user-supplied string for a Drive ``q`` single-quoted literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def file_type_of(mime_type: str | None) -> str:
    """Map a Drive mime type to the ``file_type`` vocabulary used by search."""
    if mime_type == GOOGLE_SLIDES_MIME:
        return "google_slides"
    if mime_type in (PPTX_MIME, PPT_MIME):
        return "pptx"
    return "other"


def validate_order_by(order_by: str) -> str:
    """Validate an ``orderBy`` expression (``field`` or ``field desc``)."""
    parts = order_by.split()
    if (
        not parts
        or len(parts) > 2
        or parts[0] not in ORDER_BY_FIELDS
        or (len(parts) == 2 and parts[1] != "desc")
    ):
        allowed = ", ".join(sorted(ORDER_BY_FIELDS))
        raise ValueError(
            f"order_by must be one of [{allowed}], optionally followed by "
            f"' desc' (got {order_by!r})."
        )
    return " ".join(parts)


def build_drive_query(
    name_contains: str | None = None,
    full_text_contains: str | None = None,
    file_type: str = "all",
    owned_by_me: bool | None = None,
    created_after: str | None = None,
    modified_after: str | None = None,
) -> str:
    """Build a Drive ``files.list`` ``q`` string over presentation mime types.

    Pure function (no API call) so it is easy to unit-test.
    """
    mimes = FILE_TYPE_MIMES.get(file_type)
    if mimes is None:
        allowed = ", ".join(sorted(FILE_TYPE_MIMES))
        raise ValueError(f"file_type must be one of [{allowed}] (got {file_type!r}).")

    mime_clause = " or ".join(f"mimeType = '{mime}'" for mime in mimes)
    terms = [f"({mime_clause})", "trashed = false"]

    if name_contains:
        terms.append(f"name contains '{escape_query_value(name_contains)}'")
    if full_text_contains:
        terms.append(f"fullText contains '{escape_query_value(full_text_contains)}'")
    if owned_by_me is True:
        terms.append("'me' in owners")
    if created_after:
        terms.append(f"createdTime > '{escape_query_value(created_after)}'")
    if modified_after:
        terms.append(f"modifiedTime > '{escape_query_value(modified_after)}'")

    return " and ".join(terms)


def summarize_file(file: dict[str, Any]) -> dict[str, Any]:
    """Trim a Drive file resource to the bounded shape search returns."""
    file_id = file.get("id")
    mime_type = file.get("mimeType")
    summary: dict[str, Any] = {
        "id": file_id,
        "name": file.get("name"),
        "fileType": file_type_of(mime_type),
        "mimeType": mime_type,
        "createdTime": file.get("createdTime"),
        "modifiedTime": file.get("modifiedTime"),
        "url": file.get("webViewLink")
        or f"https://drive.google.com/file/d/{file_id}/view",
        "owners": [
            owner.get("emailAddress") or owner.get("displayName")
            for owner in file.get("owners", [])
        ],
    }
    # Only native Google Slides decks can be used directly as presentation_id;
    # PowerPoint files must go through import_presentation first.
    summary["directlyEditable"] = summary["fileType"] == "google_slides"
    return summary


def search_presentations(
    services: Services,
    name_contains: str | None = None,
    full_text_contains: str | None = None,
    file_type: str = "all",
    order_by: str = "modifiedTime desc",
    max_results: int = 20,
    owned_by_me: bool | None = None,
    created_after: str | None = None,
    modified_after: str | None = None,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Search Drive for Slides/PowerPoint files (1 API call).

    Returns ``{files: [...], nextPageToken?}`` with a trimmed entry per file.
    """
    query = build_drive_query(
        name_contains=name_contains,
        full_text_contains=full_text_contains,
        file_type=file_type,
        owned_by_me=owned_by_me,
        created_after=created_after,
        modified_after=modified_after,
    )
    page_size = max(1, min(int(max_results), _MAX_RESULTS_CAP))
    response = execute(
        services.drive.files().list(
            q=query,
            orderBy=validate_order_by(order_by),
            pageSize=page_size,
            fields=_LIST_FIELDS,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
    )
    result: dict[str, Any] = {
        "files": [summarize_file(f) for f in response.get("files", [])],
    }
    next_token = response.get("nextPageToken")
    if next_token:
        result["nextPageToken"] = next_token
    return result


def import_presentation(
    services: Services,
    file_id: str,
    title: str | None = None,
    parent_folder_id: str | None = None,
) -> dict[str, Any]:
    """Convert a PowerPoint file in Drive to a native Google Slides deck (1 call).

    Drive ``files.copy`` with a target mime type performs the conversion; the
    original file is left untouched. Copying an already-native deck also works
    (equivalent to :func:`template.copy_presentation`).
    """
    body: dict[str, Any] = {"mimeType": GOOGLE_SLIDES_MIME}
    if title:
        body["name"] = title
    if parent_folder_id:
        body["parents"] = [parent_folder_id]
    result = execute(
        services.drive.files().copy(
            fileId=file_id,
            body=body,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
    )
    new_pid = result["id"]
    return {
        "presentationId": new_pid,
        "title": result.get("name"),
        "url": result.get("webViewLink")
        or f"https://docs.google.com/presentation/d/{new_pid}/edit",
        "sourceFileId": file_id,
    }
