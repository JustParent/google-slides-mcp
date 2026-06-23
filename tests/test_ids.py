import re

from google_slides_mcp import ids

VALID = re.compile(r"^[a-zA-Z0-9_-]{5,50}$")


def test_new_id_format_and_uniqueness():
    generated = {ids.new_id("slide") for _ in range(1000)}
    assert len(generated) == 1000  # collision-free in practice
    for gid in generated:
        assert VALID.match(gid)
        assert gid.startswith("slide_")


def test_new_id_sanitizes_prefix():
    gid = ids.new_id("My Shape!!")
    assert VALID.match(gid)
    assert gid.startswith("myshape_")


def test_new_id_empty_prefix_falls_back():
    gid = ids.new_id("")
    assert VALID.match(gid)
    assert gid.startswith("g_")


def test_new_id_length_capped():
    gid = ids.new_id("x" * 80)
    assert len(gid) <= 50


def test_remap():
    mapping = ids.remap(["a", "b", "c"])
    assert set(mapping) == {"a", "b", "c"}
    assert len(set(mapping.values())) == 3
