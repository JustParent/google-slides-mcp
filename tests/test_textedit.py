"""Setting an element's text with a minimal edit, so review comments stay anchored."""

import random

import pytest
from fakes import BODY, deck, fake_services, http_error, shape, slide, table

from google_slides_mcp import textedit
from google_slides_mcp.comments import utf16_len

REVISION_ERROR = "The required revision ID 'rev7' does not match the latest revision."


# --------------------------------------------------------------------------- #
# A small model of Slides text, to check what the requests really do
# --------------------------------------------------------------------------- #
def apply(old, requests):
    """Apply insertText / deleteText requests to ``old`` the way Slides does (UTF-16 indexes)."""
    units = old.encode("utf-16-le")  # two bytes per code unit
    for request in requests:
        ((kind, body),) = request.items()
        if kind == "insertText":
            at = body["insertionIndex"]
            assert 0 <= at <= len(units) // 2, f"insertion index {at} out of range"
            units = units[: 2 * at] + body["text"].encode("utf-16-le") + units[2 * at :]
        else:
            assert kind == "deleteText"
            bounds = body["textRange"]
            assert bounds["type"] == "FIXED_RANGE"
            start, end = bounds["startIndex"], bounds["endIndex"]
            assert 0 <= start < end <= len(units) // 2, f"delete range {start}..{end} out of range"
            units = units[: 2 * start] + units[2 * end :]
    return units.decode("utf-16-le")


def track(anchor, requests):
    """Where a comment anchored to ``anchor`` (start, end) ends up, or None if it is orphaned.

    Models how the live API treated anchors (verified with probes): an insertion before
    or exactly at the start shifts it, inside it grows it, at or after its end leaves it
    alone; a deletion shrinks it by the overlap, and one that removes all of it orphans it.
    """
    start, end = anchor
    for request in requests:
        ((kind, body),) = request.items()
        if kind == "insertText":
            at, size = body["insertionIndex"], utf16_len(body["text"])
            if at <= start:
                start, end = start + size, end + size
            elif at < end:
                end += size
        else:
            cut_start, cut_end = body["textRange"]["startIndex"], body["textRange"]["endIndex"]
            start -= max(0, min(cut_end, start) - cut_start)
            end -= max(0, min(cut_end, end) - cut_start)
        if start >= end:
            return None
    return start, end


def naive(old):
    """What the tool used to do: delete everything, then type the new text."""
    return [{"deleteText": {"objectId": "x", "textRange": {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": utf16_len(old)}}}]


# --------------------------------------------------------------------------- #
# edit_requests
# --------------------------------------------------------------------------- #
def test_equal_text_needs_no_requests():
    assert textedit.edit_requests("x", "Same", "Same") == []
    assert textedit.edit_requests("x", "", "") == []


def test_pure_insertions_and_deletions_touch_only_that_text():
    assert textedit.edit_requests("x", "Hello", "Hello world") == [
        {"insertText": {"objectId": "x", "insertionIndex": 5, "text": " world"}}
    ]
    assert textedit.edit_requests("x", "Hello world", "Hello") == [
        {"deleteText": {"objectId": "x", "textRange": {"type": "FIXED_RANGE", "startIndex": 5, "endIndex": 11}}}
    ]
    assert textedit.edit_requests("x", "", "Hi")[0]["insertText"]["insertionIndex"] == 0


def test_single_changed_character_is_deleted_then_replaced():
    requests = textedit.edit_requests("x", "Revenue grew 12% a year", "Revenue grew 15% a year")
    assert [list(r) for r in requests] == [["deleteText"], ["insertText"]]
    assert requests[0]["deleteText"]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 14, "endIndex": 15}
    assert requests[1]["insertText"] == {"objectId": "x", "insertionIndex": 14, "text": "5"}


def test_replaced_block_is_inserted_inside_the_old_text_then_trimmed():
    old, new = "Sales rose significantly in Q3", "Sales rose 14% in Q3"
    requests = textedit.edit_requests("x", old, new)
    # insert after the first old character, drop the old tail, then the old head
    assert [(list(r)[0], r[list(r)[0]].get("insertionIndex"), r[list(r)[0]].get("textRange")) for r in requests] == [
        ("insertText", 12, None),
        ("deleteText", None, {"type": "FIXED_RANGE", "startIndex": 15, "endIndex": 27}),
        ("deleteText", None, {"type": "FIXED_RANGE", "startIndex": 11, "endIndex": 12}),
    ]
    assert apply(old, requests) == new


def test_table_cell_requests_carry_the_cell_location():
    cell = {"rowIndex": 1, "columnIndex": 0}
    requests = textedit.edit_requests("tbl", "Revenue", "Revenue (USD)", cell)
    assert requests == [
        {"insertText": {"objectId": "tbl", "cellLocation": cell, "insertionIndex": 7, "text": " (USD)"}}
    ]
    for request in textedit.edit_requests("tbl", "abc def", "xyz uvw", cell):
        assert next(iter(request.values()))["cellLocation"] == cell


def test_emoji_indexes_are_utf16():
    old, new = "\U0001F600 grew 12% today", "\U0001F600 grew 15% today"
    requests = textedit.edit_requests("x", old, new)
    # "\U0001F600 grew 1" is 8 code points but 9 UTF-16 units (the emoji counts 2)
    assert requests[0]["deleteText"]["textRange"]["startIndex"] == 9
    assert apply(old, requests) == new


def test_requests_always_produce_exactly_the_new_text():
    rng = random.Random(20261015)
    alphabet = ["a", "b", "c", " ", "\n", "\U0001F600", "é"]
    for _ in range(4000):
        old = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 10)))
        new = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 10)))
        assert apply(old, textedit.edit_requests("x", old, new)) == new, (old, new)


def test_word_like_text_round_trips_too():
    rng = random.Random(7)
    vocabulary = ["Sales", "rose", "12%", "in", "Q3", "Revenue", "fell", "\U0001F600", "—", "(est.)", "\n", "x_y", "a"]
    separators = [" ", "  ", ", ", "\n", ""]
    for _ in range(3000):
        old = "".join(rng.choice(vocabulary) + rng.choice(separators) for _ in range(rng.randint(0, 8)))
        new = "".join(rng.choice(vocabulary) + rng.choice(separators) for _ in range(rng.randint(0, 8)))
        assert apply(old, textedit.edit_requests("x", old, new)) == new, (old, new)


def test_changed_spots_are_found_word_by_word_then_trimmed_inside_words():
    assert textedit.changed_spots("same", "same") == []
    assert textedit.changed_spots("Sales rose 12% in Q3", "Revenue rose 12% in Q4") == [
        (0, "Sales", "Revenue"),
        (19, "3", "4"),  # Q3 -> Q4: only the digit
    ]


def test_separate_changes_are_edited_last_to_first_so_offsets_stay_valid():
    requests = textedit.edit_requests("x", "Sales rose 12% in Q3", "Revenue rose 12% in Q4")
    assert requests[0]["deleteText"]["textRange"]["startIndex"] == 19  # the later change goes first
    assert apply("Sales rose 12% in Q3", requests) == "Revenue rose 12% in Q4"


def test_too_large_a_diff_falls_back_to_one_block(monkeypatch):
    monkeypatch.setattr(textedit, "_MAX_TOKEN_PAIRS", 0)
    assert len(textedit.changed_spots("Sales rose 12% in Q3", "Revenue rose 12% in Q4")) == 1
    requests = textedit.edit_requests("x", "Sales rose 12% in Q3", "Revenue rose 12% in Q4")
    assert apply("Sales rose 12% in Q3", requests) == "Revenue rose 12% in Q4"


# --------------------------------------------------------------------------- #
# What that does to comments (using the model of the live API's anchor behaviour)
# --------------------------------------------------------------------------- #
def covered(text, anchor):
    start, end = anchor
    return text.encode("utf-16-le")[2 * start : 2 * end].decode("utf-16-le")


def test_comment_on_a_rewritten_phrase_stays_on_the_new_phrase():
    old, new = "Sales rose significantly in Q3", "Sales rose 14% in Q3"
    anchor = (11, 24)  # "significantly"
    assert covered(old, anchor) == "significantly"
    requests = textedit.edit_requests("x", old, new)
    kept = track(anchor, requests)
    assert kept is not None and covered(new, kept) == "14%"
    # Deleting everything and retyping, as the tool used to, orphans the comment.
    assert track(anchor, naive(old)) is None


def test_comment_on_words_between_two_changes_survives_both():
    old, new = "Sales rose 12% in Q3", "Revenue rose 12% in Q4"
    anchor = (11, 14)  # "12%"
    assert covered(old, anchor) == "12%"
    kept = track(anchor, textedit.edit_requests("x", old, new))
    assert kept is not None and covered(new, kept) == "12%"


def test_commented_word_kept_inside_a_larger_rewrite_keeps_its_comment():
    old, new = "A Revenue B", "Revenue"
    kept = track((2, 9), textedit.edit_requests("x", old, new))  # "Revenue"
    assert kept is not None and covered(new, kept) == "Revenue"


def test_comment_on_unchanged_words_is_untouched_and_on_a_partly_changed_phrase_survives():
    old, new = "Revenue grew 12% year on year", "Revenue grew 15% year on year"
    kept = track((13, 16), textedit.edit_requests("x", old, new))  # "12%" -> "15%"
    assert kept is not None and covered(new, kept) == "15%"
    other = track((0, 7), textedit.edit_requests("x", old, new))  # "Revenue"
    assert other == (0, 7)


# --------------------------------------------------------------------------- #
# set_element_text
# --------------------------------------------------------------------------- #
def edit_deck(revision="rev7", text=BODY):
    return deck(
        [
            slide(
                "s1",
                [
                    shape("body", text),
                    shape("empty"),
                    table("tbl", [["Metric", "Value"], ["Revenue", "12% up"]]),
                    {"objectId": "pic", "image": {}},
                ],
            )
        ],
        revision=revision,
    )


def test_edit_reads_once_then_writes_against_that_revision():
    services, slides = fake_services(edit_deck(), [{"replies": []}])
    result = textedit.set_element_text(services, "pid", "body", "Revenue grew 15% year on year")
    assert len(slides.of("get")) == 1 and len(slides.of("batchUpdate")) == 1
    (write,) = slides.of("batchUpdate")
    assert write["body"]["writeControl"] == {"requiredRevisionId": "rev7"}
    assert [list(r) for r in write["body"]["requests"]] == [["deleteText"], ["insertText"]]
    assert result == {"objectId": "body", "length": 29}


def test_unchanged_text_writes_nothing():
    services, slides = fake_services(edit_deck())
    result = textedit.set_element_text(services, "pid", "body", BODY)
    assert result["unchanged"] is True
    assert slides.of("batchUpdate") == []


def test_empty_shape_just_gets_the_text():
    services, slides = fake_services(edit_deck(), [{"replies": []}])
    textedit.set_element_text(services, "pid", "empty", "Hello")
    assert slides.of("batchUpdate")[0]["body"]["requests"] == [
        {"insertText": {"objectId": "empty", "insertionIndex": 0, "text": "Hello"}}
    ]


def test_table_cell_is_addressed_by_row_and_column():
    services, slides = fake_services(edit_deck(), [{"replies": []}])
    textedit.set_element_text(services, "pid", "tbl", "Revenue (USD)", row=1, column=0)
    (request,) = slides.of("batchUpdate")[0]["body"]["requests"]
    assert request["insertText"]["cellLocation"] == {"rowIndex": 1, "columnIndex": 0}


@pytest.mark.parametrize(
    "element_id, kwargs, message",
    [
        ("nope", {}, "not found"),
        ("pic", {}, "holds no text"),
        ("tbl", {}, "is a table"),
        ("tbl", {"row": 5, "column": 0}, "no cell at row 5"),
        ("body", {"row": 0, "column": 0}, "not a table"),
        ("tbl", {"row": 1}, "both `row` and `column`"),
    ],
)
def test_bad_targets_are_explained_before_any_write(element_id, kwargs, message):
    services, slides = fake_services(edit_deck())
    with pytest.raises(ValueError, match=message):
        textedit.set_element_text(services, "pid", element_id, "New", **kwargs)
    assert slides.of("batchUpdate") == []


def test_the_notes_shape_can_be_edited_by_id():
    notes_deck = deck([slide("s1", [], notes="Old notes")], revision="rev7")
    services, slides = fake_services(notes_deck, [{"replies": []}])
    textedit.set_element_text(services, "pid", "s1_notes", "New notes")
    assert slides.of("batchUpdate")


def test_edit_is_recomputed_once_if_the_deck_changed_in_between():
    first = edit_deck("rev7", BODY)
    second = edit_deck("rev8", "Draft: Revenue grew 12% year on year")  # someone edited meanwhile
    services, slides = fake_services([first, second], [http_error(400, REVISION_ERROR), {"replies": []}])
    textedit.set_element_text(services, "pid", "body", "Draft: Revenue grew 15% year on year")
    assert len(slides.of("get")) == 2 and len(slides.of("batchUpdate")) == 2
    retry = slides.of("batchUpdate")[1]["body"]
    assert retry["writeControl"] == {"requiredRevisionId": "rev8"}
    # recomputed against the new text: only the one digit changes
    assert retry["requests"][0]["deleteText"]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 21, "endIndex": 22}


def test_a_second_revision_conflict_is_not_retried_again():
    services, slides = fake_services(edit_deck(), [http_error(400, REVISION_ERROR), http_error(400, REVISION_ERROR)])
    with pytest.raises(Exception) as raised:
        textedit.set_element_text(services, "pid", "body", "Something else entirely")
    assert "does not match the latest revision" in str(raised.value)
    assert len(slides.of("get")) == 2 and len(slides.of("batchUpdate")) == 2


def test_other_api_errors_are_not_retried():
    services, slides = fake_services(edit_deck(), [http_error(400, "Invalid requests[0].deleteText: bad range")])
    with pytest.raises(Exception, match="bad range"):
        textedit.set_element_text(services, "pid", "body", "Something else entirely")
    assert len(slides.of("get")) == 1 and len(slides.of("batchUpdate")) == 1
