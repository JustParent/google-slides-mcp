from google_slides_mcp import units


def test_pt_emu_roundtrip():
    assert units.pt_to_emu(1) == 12700
    assert units.emu_to_pt(12700) == 1
    assert units.pt_to_emu(72) == units.EMU_PER_INCH


def test_to_pt_units():
    assert units.to_pt({"magnitude": 12700, "unit": "EMU"}) == 1
    assert units.to_pt({"magnitude": 50, "unit": "PT"}) == 50
    assert units.to_pt(None) is None
    assert units.to_pt({}) is None


def test_build_transform_defaults():
    t = units.build_transform(translate_x_pt=10, translate_y_pt=20)
    assert t["unit"] == "PT"
    assert t["scaleX"] == 1.0 and t["scaleY"] == 1.0
    assert t["translateX"] == 10 and t["translateY"] == 20


def test_bounding_box_simple():
    size = {
        "width": {"magnitude": 100, "unit": "PT"},
        "height": {"magnitude": 50, "unit": "PT"},
    }
    transform = {
        "scaleX": 2.0,
        "scaleY": 1.0,
        "translateX": 10,
        "translateY": 5,
        "unit": "PT",
    }
    box = units.bounding_box(size, transform)
    assert box == {"x": 10, "y": 5, "width": 200.0, "height": 50.0}


def test_bounding_box_emu_translate():
    size = {
        "width": {"magnitude": 12700, "unit": "EMU"},  # 1 pt
        "height": {"magnitude": 12700, "unit": "EMU"},
    }
    transform = {
        "scaleX": 1.0,
        "scaleY": 1.0,
        "translateX": 12700,  # EMU -> 1 pt
        "translateY": 25400,  # EMU -> 2 pt
        "unit": "EMU",
    }
    box = units.bounding_box(size, transform)
    assert box["x"] == 1.0 and box["y"] == 2.0
    assert box["width"] == 1.0 and box["height"] == 1.0


def test_bounding_box_missing():
    assert units.bounding_box(None, {}) is None
    assert units.bounding_box({}, None) is None
