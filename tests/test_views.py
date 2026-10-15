from google_slides_mcp import views


def _text_shape(object_id, text, placeholder_type=None):
    shape = {
        "text": {
            "textElements": [
                {"textRun": {"content": text}},
            ]
        }
    }
    if placeholder_type:
        shape["placeholder"] = {"type": placeholder_type, "index": 0}
    return {
        "objectId": object_id,
        "shape": shape,
        "size": {
            "width": {"magnitude": 100, "unit": "PT"},
            "height": {"magnitude": 20, "unit": "PT"},
        },
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 5, "translateY": 6, "unit": "PT"},
    }


def test_summarize_element_text_and_box():
    element = _text_shape("t1", "Hello world", placeholder_type="TITLE")
    summary = views.summarize_element(element)
    assert summary["objectId"] == "t1"
    assert summary["type"] == "shape"
    assert summary["text"] == "Hello world"
    assert summary["placeholder"] == {"type": "TITLE", "index": 0}
    assert summary["box_pt"]["x"] == 5 and summary["box_pt"]["width"] == 100.0


def test_text_snippet_truncated():
    element = _text_shape("t2", "x" * 1000)
    summary = views.summarize_element(element)
    assert len(summary["text"]) <= views.MAX_TEXT_SNIPPET + 1
    assert summary["text"].endswith("…")


def test_summarize_page_truncates_elements():
    elements = [_text_shape(f"e{i}", f"text {i}") for i in range(views.MAX_ELEMENTS_PER_PAGE + 5)]
    page = {"objectId": "p1", "pageType": "SLIDE", "pageElements": elements}
    summary = views.summarize_page(page)
    assert summary["elementCount"] == views.MAX_ELEMENTS_PER_PAGE + 5
    assert len(summary["elements"]) == views.MAX_ELEMENTS_PER_PAGE
    assert summary["elementsTruncated"] is True


def test_summarize_presentation_basic():
    presentation = {
        "presentationId": "pid",
        "title": "Deck",
        "pageSize": {
            "width": {"magnitude": 720, "unit": "PT"},
            "height": {"magnitude": 405, "unit": "PT"},
        },
        "slides": [
            {"objectId": "s1", "pageType": "SLIDE", "pageElements": [_text_shape("e1", "Hi")]},
        ],
        "masters": [{"objectId": "m1", "pageType": "MASTER", "pageElements": []}],
        "layouts": [{"objectId": "l1", "pageType": "LAYOUT", "pageElements": []}],
    }
    summary = views.summarize_presentation(presentation)
    assert summary["slideCount"] == 1
    assert summary["pageSize_pt"] == {"width": 720.0, "height": 405.0}
    assert "masters" not in summary  # excluded by default

    with_masters = views.summarize_presentation(presentation, include_masters=True)
    assert len(with_masters["masters"]) == 1
    assert len(with_masters["layouts"]) == 1


def test_summary_stays_lean_without_comments():
    presentation = {
        "slides": [{"objectId": "s1", "pageType": "SLIDE", "pageElements": []}],
        "comments": [],
    }
    summary = views.summarize_presentation(presentation)
    assert "comments" not in summary
    assert "openComments" not in summary["slides"][0]


def test_summary_counts_open_comments_overall_and_per_slide():
    anchor = {"anchorId": "a1", "objectAnchors": [{"objectId": "s2"}]}
    presentation = {
        "slides": [
            {"objectId": "s1", "pageType": "SLIDE", "pageElements": []},
            {"objectId": "s2", "pageType": "SLIDE", "pageElements": [], "commentAnchors": [anchor]},
        ],
        "comments": [
            {"commentId": "c1", "anchorId": "a1", "status": "OPEN"},
            {"commentId": "c2", "anchorId": "a1", "status": "RESOLVED"},
            {"commentId": "c3", "anchorId": "gone", "status": "OPEN"},
        ],
    }
    summary = views.summarize_presentation(presentation)
    assert summary["comments"] == {"open": 2, "resolved": 1, "orphaned": 1}
    assert [s.get("openComments") for s in summary["slides"]] == [None, 1]


def test_page_summary_counts_the_open_threads_of_a_pages_get_response():
    page = {
        "objectId": "s1",
        "pageElements": [],
        "comments": [{"status": "OPEN"}, {"status": "RESOLVED"}, {"status": "OPEN"}],
    }
    assert views.summarize_page(page)["openComments"] == 2
    assert "openComments" not in views.summarize_page({"objectId": "s1", "pageElements": []})
