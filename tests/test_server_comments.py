"""The MCP tools themselves: that they ask Google for comments, and pass things through.

``get_services`` is replaced with a fake Slides service; the tool functions are called
directly. This is the layer where a forgotten ``commentsViewMode`` would silently make
every comment disappear.
"""

import pytest
from fakes import commented_deck, deck, fake_services, inserted, shape, slide, thread

from google_slides_mcp import comments, server


@pytest.fixture
def install(monkeypatch):
    def _install(deck_=None, batch_responses=None):
        services, slides = fake_services(deck_, batch_responses)
        monkeypatch.setattr(server, "get_services", lambda: services)
        return slides

    return _install


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def test_get_presentation_summary_asks_for_comments_and_counts_them(install):
    slides = install(commented_deck())
    summary = server.get_presentation("pid")
    (call,) = slides.of("get")
    assert call["commentsViewMode"] == comments.DEFAULT_ACCESS  # never a 403 for view-only users
    assert summary["comments"] == {"open": 6, "resolved": 1, "orphaned": 1}
    open_by_slide = {s["objectId"]: s.get("openComments") for s in summary["slides"]}
    assert open_by_slide == {"s1": 5, "s2": 1}


def test_get_presentation_summary_is_unchanged_when_there_are_no_comments(install):
    install(deck([slide("s1", [shape("a", "Hi")])]))
    summary = server.get_presentation("pid")
    assert "comments" not in summary and "openComments" not in summary["slides"][0]


def test_get_presentation_raw_is_the_untouched_response(install):
    slides = install(commented_deck())
    raw = server.get_presentation("pid", raw=True)
    assert "commentsViewMode" not in slides.of("get")[0]
    assert raw["revisionId"] == "rev1"


def test_get_page_asks_for_comments_and_counts_open_threads(install):
    slides = install()
    slides.page = {
        "objectId": "s1",
        "pageType": "SLIDE",
        "pageElements": [],
        "comments": [thread("c1", "a", "Open"), thread("c2", "a", "Done", status="RESOLVED")],
    }
    summary = server.get_page("pid", "s1")
    assert slides.of("pages.get")[0]["commentsViewMode"] == comments.DEFAULT_ACCESS
    assert summary["openComments"] == 1
    server.get_page("pid", "s1", raw=True)
    assert "commentsViewMode" not in slides.of("pages.get")[1]


def test_list_slides_shows_which_slides_have_open_comments(install):
    slides = install(commented_deck())
    listed = server.list_slides("pid")
    assert slides.of("get")[0]["commentsViewMode"] == comments.DEFAULT_ACCESS
    assert [(s["objectId"], s.get("openComments")) for s in listed] == [("s1", 5), ("s2", 1)]


def test_list_comments_tool_passes_its_arguments_through(install):
    slides = install(commented_deck())
    result = server.list_comments("pid", page_id="s2", status="all")
    assert slides.of("get")[0]["commentsViewMode"] == comments.INCLUDED
    assert [t["commentId"] for t in result["threads"]] == ["c_text"]


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def test_batch_update_sends_only_the_requests_unless_a_revision_is_required(install):
    slides = install(batch_responses=[{"replies": [{}]}, {"replies": [{}]}])
    server.batch_update("pid", [{"deleteObject": {"objectId": "x"}}])
    server.batch_update("pid", [{"deleteObject": {"objectId": "x"}}], required_revision_id="rev5")
    first, second = (c["body"] for c in slides.of("batchUpdate"))
    assert first == {"requests": [{"deleteObject": {"objectId": "x"}}]}
    assert second == {"requests": [{"deleteObject": {"objectId": "x"}}], "writeControl": {"requiredRevisionId": "rev5"}}


def test_batch_update_returns_the_new_revision_and_comment_state(install):
    install(batch_responses=[inserted("c1", revision="rev9")])
    result = server.batch_update("pid", [{"insertComment": {"content": "x", "objectId": "s1"}}])
    assert result["revisionId"] == "rev9"
    assert result["commentUpdateState"] == "ALL_SAVED"
    assert result["replies"][0]["insertComment"]["commentThread"]["commentId"] == "c1"


def test_batch_update_warns_when_comments_were_dropped_but_the_rest_applied(install):
    install(batch_responses=[{"replies": [{}, {}], "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON"}])
    result = server.batch_update("pid", [{"deleteObject": {"objectId": "x"}}, {"insertComment": {"content": "x", "objectId": "s1"}}])
    assert any("NOT saved" in w for w in result["warnings"])


def test_batch_update_still_validates_requests(install):
    slides = install()
    with pytest.raises(ValueError, match="non-empty"):
        server.batch_update("pid", [])
    with pytest.raises(ValueError, match="exactly one"):
        server.batch_update("pid", [{"a": {}, "b": {}}])
    assert slides.calls == []


def test_set_element_text_tool_edits_minimally(install):
    slides = install(deck([slide("s1", [shape("body", "Hello world")])]), [{"replies": []}])
    result = server.set_element_text("pid", "body", "Hello there", row=None, column=None)
    assert result["objectId"] == "body"
    (write,) = slides.of("batchUpdate")
    # "world" -> "there": inserted inside the old word, then the old word trimmed away
    assert [next(iter(r)) for r in write["body"]["requests"]] == ["insertText", "deleteText", "deleteText"]
    assert write["body"]["writeControl"] == {"requiredRevisionId": "rev1"}


def test_comment_tools_are_wired_to_the_comment_functions(install):
    slides = install(batch_responses=[inserted("c1"), {"replies": [{"addCommentReply": {"post": {"postId": "p1"}}}], "commentUpdateState": "ALL_SAVED"}, {"replies": [{}], "commentUpdateState": "ALL_SAVED"}])
    assert server.add_comment("pid", "Look here", page_id="s1")["commentId"] == "c1"
    assert server.reply_to_comment("pid", "c1", content="Done", action="resolve")["status"] == "resolved"
    assert server.delete_comment("pid", "c1")["deleted"] == "thread"
    kinds = [list(c["body"]["requests"][0])[0] for c in slides.of("batchUpdate")]
    assert kinds == ["insertComment", "addCommentReply", "deleteComment"]
