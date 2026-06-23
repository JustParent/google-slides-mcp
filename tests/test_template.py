from google_slides_mcp import template


def _slide(object_id, *, skipped=None):
    props = {}
    if skipped is not None:
        props["isSkipped"] = skipped
    return {"objectId": object_id, "slideProperties": props}


def test_skipped_slide_ids_filters_only_skipped():
    presentation = {
        "slides": [
            _slide("a", skipped=True),
            _slide("b", skipped=False),
            _slide("c"),  # no isSkipped key
            _slide("d", skipped=True),
        ]
    }
    assert template.skipped_slide_ids(presentation) == ["a", "d"]


def test_skipped_slide_ids_empty():
    assert template.skipped_slide_ids({}) == []
    assert template.skipped_slide_ids({"slides": []}) == []


def test_skipped_slide_ids_ignores_missing_object_id():
    presentation = {"slides": [{"slideProperties": {"isSkipped": True}}]}
    assert template.skipped_slide_ids(presentation) == []
