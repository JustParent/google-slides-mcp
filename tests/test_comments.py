"""Review comments: reading threads with their locations, and writing them.

Everything runs against a fake Slides service (see ``fakes.py``), whose responses are
shaped like the real API's.
"""

import pytest
from fakes import (
    NOTES,
    anchor,
    commented_deck,
    deck,
    fake_services,
    http_error,
    inserted,
    lookup_deck,
    shape,
    slide,
    thread,
)

from google_slides_mcp import comments


# --------------------------------------------------------------------------- #
# UTF-16 offsets and text extraction
# --------------------------------------------------------------------------- #
def test_utf16_len_counts_surrogate_pairs():
    assert comments.utf16_len("a") == 1
    assert comments.utf16_len("\U0001F600") == 2
    assert comments.utf16_len("\U0001F600 grew") == 7


def test_slice_utf16_uses_code_units_not_code_points():
    text = "\U0001F600 grew 12% today"
    assert comments.slice_utf16(text, 8, 11) == "12%"
    assert comments.slice_utf16(text, 7, 10) == " 12"  # what code-point offsets would give


def test_text_of_joins_runs_and_auto_text_and_tolerates_none():
    content = {
        "textElements": [
            {"paragraphMarker": {}},
            {"textRun": {"content": "Slide "}},
            {"autoText": {"type": "SLIDE_NUMBER", "content": "3"}},
            {"textRun": {"content": "\n"}},
        ]
    }
    assert comments.text_of(content) == "Slide 3\n"
    assert comments.text_of(None) == ""


# --------------------------------------------------------------------------- #
# Finding quoted text
# --------------------------------------------------------------------------- #
def test_find_text_reads_shapes_table_cells_and_groups_in_order():
    matches = comments.find_text(lookup_deck(), "12%")
    assert [(m.slide_id, m.target.element_id, m.target.row, m.target.column) for m in matches] == [
        ("s1", "body", None, None),
        ("s1", "tbl", 1, 1),
        ("s1", "inner", None, None),
        ("s1", "emoji", None, None),
        ("s2", "again", None, None),
        ("s2", "again", None, None),
    ]


def test_find_text_offsets_are_utf16():
    match = comments.find_text(lookup_deck(), "12%", element_id="emoji")[0]
    assert (match.start, match.end) == (8, 11)


def test_find_text_scopes_to_a_slide_and_does_not_overlap_matches():
    assert [m.start for m in comments.find_text(lookup_deck(), "12%", page_id="s2")] == [7, 15]
    assert len(comments.find_text(deck([slide("s", [shape("x", "aaaa")])]), "aa")) == 2


def test_find_text_rejects_empty_and_leaves_speaker_notes_alone():
    with pytest.raises(ValueError, match="empty"):
        comments.find_text(lookup_deck(), "")
    assert comments.find_text(lookup_deck(), "churn") == []  # only in the notes


# --------------------------------------------------------------------------- #
# Threads and their locations
# --------------------------------------------------------------------------- #
def by_id(result):
    return {t["commentId"]: t for t in result["threads"]}


def test_each_kind_of_anchor_becomes_a_located_thread():
    threads = by_id(comments.build_threads(commented_deck(), status="all"))
    base = {"slideId": "s1", "slideIndex": 0}
    assert threads["c_slide"]["locations"] == [{"kind": "slide", **base}]
    assert threads["c_elem"]["locations"] == [{"kind": "element", **base, "elementId": "body"}]
    assert threads["c_notes"]["locations"] == [
        {"kind": "notes", **base, "elementId": "s1_notes", "start": 0, "end": 26, "text": NOTES}
    ]


def test_text_thread_reports_range_covered_text_and_every_slide_it_sits_on():
    thread_view = by_id(comments.build_threads(commented_deck()))["c_text"]
    assert thread_view["locations"] == [
        {"kind": "text", "slideId": "s1", "slideIndex": 0, "elementId": "body", "start": 13, "end": 16, "text": "12%"},
        {"kind": "text", "slideId": "s2", "slideIndex": 1, "elementId": "other", "start": 0, "end": 6, "text": "Second"},
    ]


def test_table_cell_zero_row_and_column_are_not_lost():
    # The API omits zeros: cellLocation {} is row 0, column 0.
    location = by_id(comments.build_threads(commented_deck()))["c_cells"]["locations"][0]
    assert location["kind"] == "table_cells"
    assert location["cells"] == [
        {"row": 0, "column": 0, "start": 0, "end": 6, "text": "Metric"},
        {"row": 1, "column": 1, "start": 0, "end": 3, "text": "12%"},
    ]


def test_thread_without_an_anchor_is_orphaned():
    view = comments.build_threads(commented_deck())
    orphan = by_id(view)["c_orphan"]
    assert orphan["orphaned"] is True
    assert "locations" not in orphan
    assert orphan["quote"] == "old chart"  # the quote is how you recognise it
    assert view["counts"] == {"open": 6, "resolved": 1, "orphaned": 1}


def test_thread_and_reply_details():
    threads = by_id(comments.build_threads(commented_deck()))
    assert threads["c_slide"]["mine"] is True
    assert "mine" not in threads["c_elem"]
    text_thread = threads["c_text"]
    assert text_thread["author"] == "Reviewer A" and text_thread["content"] == "Is it 12 or 14?"
    assert [r["postId"] for r in text_thread["replies"]] == ["p1", "p3"]  # the deleted reply is dropped
    assert text_thread["replies"][0]["content"] == "Checked: 12."
    assert text_thread["replies"][1]["action"] == "reopen" and "content" not in text_thread["replies"][1]
    assert text_thread["replies"][1]["mine"] is True


def test_status_filter_and_ordering():
    default = comments.build_threads(commented_deck())
    assert [t["commentId"] for t in default["threads"]] == ["c_slide", "c_elem", "c_text", "c_cells", "c_notes", "c_orphan"]
    resolved = comments.build_threads(commented_deck(), status="resolved")
    assert [t["commentId"] for t in resolved["threads"]] == ["c_done"]
    everything = comments.build_threads(commented_deck(), status="ALL")
    assert [t["commentId"] for t in everything["threads"]][-2:] == ["c_done", "c_orphan"]  # orphans last
    with pytest.raises(ValueError, match="status"):
        comments.build_threads(commented_deck(), status="closed")


def test_threads_are_ordered_by_slide_then_creation_time_with_orphans_last():
    first = slide("s1", [shape("a", "First")], anchors=[anchor("a1", {"objectId": "a"})])
    second = slide("s2", [shape("b", "Second")], anchors=[anchor("a2", {"objectId": "b"})])
    result = comments.build_threads(
        deck(
            [first, second],
            [
                thread("early_orphan", "gone", "x", created="2026-10-01T08:00:00Z"),
                thread("on_s2", "a2", "x", created="2026-10-01T09:00:00Z"),
                thread("on_s1_late", "a1", "x", created="2026-10-01T11:00:00Z"),
                thread("on_s1_early", "a1", "x", created="2026-10-01T10:00:00Z"),
            ],
        )
    )
    # slide order beats creation time; an orphan has no slide, so it goes last whenever it was made
    assert [t["commentId"] for t in result["threads"]] == ["on_s1_early", "on_s1_late", "on_s2", "early_orphan"]


def test_page_filter_keeps_threads_anchored_on_that_slide():
    second = comments.build_threads(commented_deck(), page_id="s2")
    assert [t["commentId"] for t in second["threads"]] == ["c_text"]
    assert second["counts"] == {"open": 1, "resolved": 0, "orphaned": 0}
    first = comments.build_threads(commented_deck(), page_id="s1", status="all")
    assert "c_orphan" not in by_id(first)  # orphans belong to no slide
    with pytest.raises(ValueError, match="not found"):
        comments.build_threads(commented_deck(), page_id="nope")


def test_output_is_bounded(monkeypatch):
    monkeypatch.setattr(comments, "MAX_THREADS", 2)
    result = comments.build_threads(commented_deck())
    assert len(result["threads"]) == 2 and result["threadsTruncated"] is True
    assert result["counts"]["open"] == 6  # counts still describe everything in scope


def test_summarize_counts_open_threads_per_slide():
    summary = comments.summarize(commented_deck())
    assert (summary["open"], summary["resolved"], summary["orphaned"]) == (6, 1, 1)
    assert summary["bySlide"] == {"s1": 5, "s2": 1}  # c_text sits on both slides; c_done is resolved
    assert comments.summarize(deck([slide("s1")])) is None
    assert comments.summarize(deck([slide("s1")], [])) is None


# --------------------------------------------------------------------------- #
# list_comments
# --------------------------------------------------------------------------- #
def test_list_comments_asks_for_comments_and_returns_the_revision():
    services, slides = fake_services(commented_deck())
    result = comments.list_comments(services, "pid")
    (call,) = slides.of("get")
    # Without this parameter the API returns no comments at all.
    assert call == {"presentationId": "pid", "commentsViewMode": comments.INCLUDED}
    assert result["revisionId"] == "rev1" and result["presentationId"] == "pid"
    assert result["counts"]["open"] == 6


def test_list_comments_explains_a_403():
    services, _ = fake_services(http_error(403, "The caller does not have permission"))
    with pytest.raises(ValueError, match="comment access") as raised:
        comments.list_comments(services, "pid")
    assert "does not have permission" in str(raised.value)


def test_list_comments_lets_other_errors_through():
    services, _ = fake_services(http_error(404, "Requested entity was not found."))
    with pytest.raises(Exception) as raised:
        comments.list_comments(services, "pid")
    assert not isinstance(raised.value, ValueError)


# --------------------------------------------------------------------------- #
# add_comment
# --------------------------------------------------------------------------- #
def written(slides):
    """The single insertComment request that was sent."""
    (call,) = slides.of("batchUpdate")
    assert call["presentationId"] == "pid" and list(call["body"]) == ["requests"]
    (request,) = call["body"]["requests"]
    return request["insertComment"]


def test_comment_on_a_slide_is_one_write_and_no_read():
    services, slides = fake_services(batch_responses=[inserted("c9", quote="Quarterly review")])
    result = comments.add_comment(services, "pid", "Retitle this", page_id="s1")
    assert written(slides) == {"content": "Retitle this", "objectId": "s1"}
    assert slides.of("get") == []
    assert result == {
        "commentId": "c9", "status": "open", "quote": "Quarterly review", "revisionId": "rev2",
        "location": {"kind": "slide", "slideId": "s1"},
    }


def test_comment_on_an_element():
    services, slides = fake_services(batch_responses=[inserted()])
    result = comments.add_comment(services, "pid", "Check", element_id="body")
    assert written(slides) == {"content": "Check", "objectId": "body"}
    assert result["location"] == {"kind": "element", "elementId": "body"}


def test_comment_on_quoted_text_reads_once_then_writes_the_exact_range():
    services, slides = fake_services(lookup_deck(), [inserted("c1", quote="12%")])
    result = comments.add_comment(services, "pid", "Is it 14?", text="12%", element_id="body")
    assert len(slides.of("get")) == 1 and len(slides.of("batchUpdate")) == 1
    assert written(slides) == {
        "content": "Is it 14?",
        "shapeTextAnchor": {"objectId": "body", "textRange": {"type": "FIXED_RANGE", "startIndex": 13, "endIndex": 16}},
    }
    assert result["location"] == {
        "kind": "text", "slideId": "s1", "slideIndex": 0, "elementId": "body", "start": 13, "end": 16, "text": "12%",
    }


def test_comment_on_text_inside_a_table_cell():
    services, slides = fake_services(lookup_deck(), [inserted()])
    result = comments.add_comment(services, "pid", "Units?", text="12%", occurrence=2)
    assert written(slides)["tableCellTextAnchor"] == {
        "objectId": "tbl",
        "cellLocation": {"rowIndex": 1, "columnIndex": 1},
        "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 3},
    }
    assert result["location"]["cells"] == [{"row": 1, "column": 1, "start": 0, "end": 3, "text": "12%"}]


def test_quoted_text_after_an_emoji_uses_utf16_offsets():
    services, slides = fake_services(lookup_deck(), [inserted()])
    comments.add_comment(services, "pid", "x", text="12%", occurrence=4)  # the emoji box
    assert written(slides)["shapeTextAnchor"]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 8, "endIndex": 11}


def test_ambiguous_quote_lists_the_candidates_and_writes_nothing():
    services, slides = fake_services(lookup_deck())
    with pytest.raises(ValueError, match="6 times") as raised:
        comments.add_comment(services, "pid", "x", text="12%")
    message = str(raised.value)
    assert "occurrence" in message and "element tbl, row 1 column 1" in message and "slide index 1 (s2)" in message
    assert slides.of("batchUpdate") == []


def test_quote_can_be_narrowed_by_slide_element_or_cell():
    services, slides = fake_services(lookup_deck(), [inserted(), inserted()])
    comments.add_comment(services, "pid", "x", text="12%", page_id="s2", occurrence=2)
    assert written_at(slides, 0)["shapeTextAnchor"]["textRange"]["startIndex"] == 15
    comments.add_comment(services, "pid", "x", text="12%", element_id="tbl", row=1, column=1)
    assert written_at(slides, 1)["tableCellTextAnchor"]["cellLocation"] == {"rowIndex": 1, "columnIndex": 1}


def written_at(slides, index):
    return slides.of("batchUpdate")[index]["body"]["requests"][0]["insertComment"]


def test_missing_quote_and_out_of_range_occurrence():
    services, slides = fake_services(lookup_deck())
    with pytest.raises(ValueError, match="not found"):
        comments.add_comment(services, "pid", "x", text="absent")
    with pytest.raises(ValueError, match="only 1 time"):
        comments.add_comment(services, "pid", "x", text="Quarterly", occurrence=2)
    assert slides.of("batchUpdate") == []


def test_comment_on_speaker_notes():
    services, slides = fake_services(lookup_deck(), [inserted()])
    result = comments.add_comment(services, "pid", "Soften this", notes=True, page_id="s1")
    assert written(slides)["shapeTextAnchor"] == {"objectId": "s1_notes", "textRange": {"type": "ALL"}}
    assert result["location"] == {"kind": "notes", "slideId": "s1", "slideIndex": 0, "elementId": "s1_notes"}


def test_comment_on_quoted_speaker_notes_is_case_sensitive():
    services, slides = fake_services(lookup_deck(), [inserted()])
    result = comments.add_comment(services, "pid", "x", notes=True, page_id="s1", text="Churn")
    assert written(slides)["shapeTextAnchor"] == {
        "objectId": "s1_notes", "textRange": {"type": "FIXED_RANGE", "startIndex": 27, "endIndex": 32},
    }
    assert result["location"]["kind"] == "notes" and result["location"]["text"] == "Churn"


def test_speaker_notes_that_are_missing_or_empty_are_reported():
    services, _ = fake_services(deck([slide("s1"), slide("s2", notes="")]))
    with pytest.raises(ValueError, match="no speaker notes"):
        comments.add_comment(services, "pid", "x", notes=True, page_id="s1")
    with pytest.raises(ValueError, match="no speaker notes"):
        comments.add_comment(services, "pid", "x", notes=True, page_id="s2")
    with pytest.raises(ValueError, match="not found"):
        comments.add_comment(services, "pid", "x", notes=True, page_id="nope")


def test_comment_on_a_cell_and_on_a_block_of_cells_need_no_read():
    services, slides = fake_services(batch_responses=[inserted(), inserted()])
    cell = comments.add_comment(services, "pid", "x", element_id="tbl", row=0, column=1)
    assert written_at(slides, 0)["tableCellTextAnchor"] == {
        "objectId": "tbl", "cellLocation": {"rowIndex": 0, "columnIndex": 1}, "textRange": {"type": "ALL"},
    }
    assert cell["location"] == {"kind": "table_cells", "elementId": "tbl", "cells": [{"row": 0, "column": 1}]}
    block = comments.add_comment(services, "pid", "x", element_id="tbl", row=0, column=0, row_span=2)
    assert written_at(slides, 1)["tableAnchor"] == {
        "objectId": "tbl",
        "tableRange": {"location": {"rowIndex": 0, "columnIndex": 0}, "rowSpan": 2, "columnSpan": 1},
    }
    assert block["location"]["kind"] == "table_range"
    assert slides.of("get") == []


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"content": "  "}, "content"),
        ({"content": "x"}, "Say where"),
        ({"content": "x", "page_id": "s1", "occurrence": 0}, "counts from 1"),
        ({"content": "x", "element_id": "tbl", "row": 1}, "both `row` and `column`"),
        ({"content": "x", "element_id": "tbl", "row_span": 2}, "need"),
        ({"content": "x", "element_id": "tbl", "row": 0, "column": 0, "row_span": 2, "text": "Metric"}, "can't also quote"),
        ({"content": "x", "row": 0, "column": 0}, "element_id"),
        ({"content": "x", "notes": True}, "page_id"),
        ({"content": "x", "notes": True, "page_id": "s1", "element_id": "title"}, "take only"),
        ({"content": "x", "text": ""}, "empty"),
    ],
)
def test_add_comment_rejects_bad_requests_before_touching_the_api(kwargs, message):
    services, slides = fake_services(lookup_deck())
    with pytest.raises(ValueError, match=message):
        comments.add_comment(services, "pid", **kwargs)
    assert slides.of("batchUpdate") == [], "a rejected request must not write anything"


def test_unsaved_comment_is_an_error_not_a_success():
    failed = {"replies": [{}], "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON"}
    services, _ = fake_services(batch_responses=[failed])
    with pytest.raises(comments.CommentNotSavedError, match="did not save"):
        comments.add_comment(services, "pid", "x", page_id="s1")


def test_success_without_a_thread_in_the_reply_is_an_error():
    services, _ = fake_services(batch_responses=[{"replies": [{}], "commentUpdateState": "ALL_SAVED"}])
    with pytest.raises(RuntimeError, match="no comment thread"):
        comments.add_comment(services, "pid", "x", page_id="s1")


# --------------------------------------------------------------------------- #
# reply, resolve, delete
# --------------------------------------------------------------------------- #
def reply_response(post_id="p9", revision="rev3"):
    return {
        "replies": [{"addCommentReply": {"post": {"postId": post_id}}}],
        "commentUpdateState": "ALL_SAVED",
        "writeControl": {"requiredRevisionId": revision},
    }


def sent(slides):
    (call,) = slides.of("batchUpdate")
    (request,) = call["body"]["requests"]
    return request


def test_plain_reply():
    services, slides = fake_services(batch_responses=[reply_response()])
    result = comments.reply_to_comment(services, "pid", "c1", content="Done, now 14%.")
    assert sent(slides) == {"addCommentReply": {"commentId": "c1", "post": {"content": "Done, now 14%."}}}
    assert result == {"commentId": "c1", "postId": "p9", "revisionId": "rev3"}


@pytest.mark.parametrize("action, status", [("resolve", "resolved"), ("REOPEN", "open")])
def test_resolve_and_reopen(action, status):
    services, slides = fake_services(batch_responses=[reply_response()])
    result = comments.reply_to_comment(services, "pid", "c1", action=action)
    assert sent(slides) == {"addCommentReply": {"commentId": "c1", "post": {"commentAction": action.upper()}}}
    assert result["status"] == status


def test_reply_and_resolve_in_one_post():
    services, slides = fake_services(batch_responses=[reply_response()])
    comments.reply_to_comment(services, "pid", "c1", content="Fixed.", action="resolve")
    assert sent(slides)["addCommentReply"]["post"] == {"content": "Fixed.", "commentAction": "RESOLVE"}


def test_reply_needs_content_or_an_action():
    services, slides = fake_services()
    with pytest.raises(ValueError, match="content"):
        comments.reply_to_comment(services, "pid", "c1", content="  ")
    with pytest.raises(ValueError, match="action"):
        comments.reply_to_comment(services, "pid", "c1", content="x", action="close")
    assert slides.calls == []


def test_unsaved_reply_is_an_error():
    services, _ = fake_services(batch_responses=[{"replies": [{}], "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON"}])
    with pytest.raises(comments.CommentNotSavedError):
        comments.reply_to_comment(services, "pid", "c1", content="x")


def test_delete_a_thread_or_one_reply():
    saved = {"replies": [{}], "commentUpdateState": "ALL_SAVED", "writeControl": {"requiredRevisionId": "rev4"}}
    services, slides = fake_services(batch_responses=[saved, saved])
    thread_result = comments.delete_comment(services, "pid", "c1")
    reply_result = comments.delete_comment(services, "pid", "c1", post_id="p2")
    requests = [c["body"]["requests"][0] for c in slides.of("batchUpdate")]
    assert requests == [
        {"deleteComment": {"commentId": "c1"}},
        {"deleteCommentReply": {"commentId": "c1", "postId": "p2"}},
    ]
    assert thread_result == {"deleted": "thread", "commentId": "c1", "revisionId": "rev4"}
    assert reply_result == {"deleted": "reply", "commentId": "c1", "postId": "p2", "revisionId": "rev4"}


# --------------------------------------------------------------------------- #
# Raw batchUpdate results
# --------------------------------------------------------------------------- #
def test_batch_summary_without_comment_requests_stays_quiet():
    response = {"replies": [{}], "commentUpdateState": "NO_UPDATES_REQUESTED", "writeControl": {"requiredRevisionId": "r9"}}
    assert comments.batch_summary(response, []) == {"replies": [{}], "revisionId": "r9"}


def test_batch_summary_reports_comment_state_and_warns_when_comments_were_not_saved():
    saved = comments.batch_summary({"replies": [], "commentUpdateState": "ALL_SAVED"}, ["w"])
    assert saved == {"replies": [], "commentUpdateState": "ALL_SAVED", "warnings": ["w"]}
    failed = comments.batch_summary({"replies": [], "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON"}, ["w"])
    assert failed["commentUpdateState"] == "ALL_FAILED_UNKNOWN_REASON"
    assert failed["warnings"][0] == "w" and "NOT saved" in failed["warnings"][1]
