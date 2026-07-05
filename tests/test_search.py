import pytest

from google_slides_mcp import search


def test_build_drive_query_defaults_cover_all_presentation_types():
    query = search.build_drive_query()
    assert "mimeType = 'application/vnd.google-apps.presentation'" in query
    assert (
        "mimeType = 'application/vnd.openxmlformats-officedocument."
        "presentationml.presentation'" in query
    )
    assert "mimeType = 'application/vnd.ms-powerpoint'" in query
    assert "trashed = false" in query


def test_build_drive_query_google_slides_only():
    query = search.build_drive_query(file_type="google_slides")
    assert query == (
        "(mimeType = 'application/vnd.google-apps.presentation') "
        "and trashed = false"
    )


def test_build_drive_query_pptx_excludes_native_slides():
    query = search.build_drive_query(file_type="pptx")
    assert "application/vnd.google-apps.presentation" not in query
    assert "application/vnd.ms-powerpoint" in query


def test_build_drive_query_filters():
    query = search.build_drive_query(
        name_contains="corporate template",
        full_text_contains="Q3",
        owned_by_me=True,
        created_after="2026-01-01T00:00:00",
        modified_after="2026-02-01T00:00:00",
    )
    assert "name contains 'corporate template'" in query
    assert "fullText contains 'Q3'" in query
    assert "'me' in owners" in query
    assert "createdTime > '2026-01-01T00:00:00'" in query
    assert "modifiedTime > '2026-02-01T00:00:00'" in query


def test_build_drive_query_escapes_quotes_and_backslashes():
    query = search.build_drive_query(name_contains="Bob's \\ deck")
    assert "name contains 'Bob\\'s \\\\ deck'" in query


def test_build_drive_query_rejects_unknown_file_type():
    with pytest.raises(ValueError, match="file_type"):
        search.build_drive_query(file_type="keynote")


def test_validate_order_by_accepts_field_and_desc():
    assert search.validate_order_by("modifiedTime desc") == "modifiedTime desc"
    assert search.validate_order_by("createdTime") == "createdTime"
    assert search.validate_order_by("name  desc") == "name desc"


@pytest.mark.parametrize(
    "order_by", ["", "sharedTime", "modifiedTime asc", "name desc extra", "name; drop"]
)
def test_validate_order_by_rejects_invalid(order_by):
    with pytest.raises(ValueError, match="order_by"):
        search.validate_order_by(order_by)


def test_file_type_of():
    assert search.file_type_of(search.GOOGLE_SLIDES_MIME) == "google_slides"
    assert search.file_type_of(search.PPTX_MIME) == "pptx"
    assert search.file_type_of(search.PPT_MIME) == "pptx"
    assert search.file_type_of("application/pdf") == "other"
    assert search.file_type_of(None) == "other"


def test_summarize_file_native_slides_is_directly_editable():
    summary = search.summarize_file(
        {
            "id": "abc",
            "name": "Q3 Review",
            "mimeType": search.GOOGLE_SLIDES_MIME,
            "createdTime": "2026-01-01T00:00:00Z",
            "modifiedTime": "2026-02-01T00:00:00Z",
            "webViewLink": "https://docs.google.com/presentation/d/abc/edit",
            "owners": [{"displayName": "David", "emailAddress": "david@example.com"}],
        }
    )
    assert summary["fileType"] == "google_slides"
    assert summary["directlyEditable"] is True
    assert summary["owners"] == ["david@example.com"]
    assert summary["url"] == "https://docs.google.com/presentation/d/abc/edit"


def test_summarize_file_pptx_needs_import():
    summary = search.summarize_file({"id": "xyz", "mimeType": search.PPTX_MIME})
    assert summary["fileType"] == "pptx"
    assert summary["directlyEditable"] is False
    # No webViewLink in the response -> falls back to a Drive URL.
    assert summary["url"] == "https://drive.google.com/file/d/xyz/view"
