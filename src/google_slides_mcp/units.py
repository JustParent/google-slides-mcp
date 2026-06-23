"""Unit conversions and affine-transform / bounding-box helpers.

The Slides API positions and sizes elements with an :class:`AffineTransform`
(scaleX, scaleY, shearX, shearY, translateX, translateY) plus an intrinsic
``size``. ``translate`` gives the **upper-left corner** of the element. These
helpers let callers think in points instead of raw matrices.

Unit facts:
    1 inch  = 914400 EMU = 72 PT
    1 PT    = 12700 EMU
"""

from __future__ import annotations

from typing import Any

EMU_PER_PT = 12700
EMU_PER_INCH = 914400


def pt_to_emu(pt: float) -> int:
    """Convert points to EMU (rounded to the nearest integer EMU)."""
    return round(pt * EMU_PER_PT)


def emu_to_pt(emu: float) -> float:
    """Convert EMU to points."""
    return emu / EMU_PER_PT


def to_pt(dimension: dict[str, Any] | None) -> float | None:
    """Normalize a Slides ``Dimension`` ({magnitude, unit}) to points.

    Returns None if the dimension is missing/empty.
    """
    if not dimension or "magnitude" not in dimension:
        return None
    magnitude = dimension["magnitude"]
    unit = dimension.get("unit", "EMU")
    if unit == "PT":
        return float(magnitude)
    if unit == "EMU":
        return emu_to_pt(magnitude)
    raise ValueError(f"Unknown dimension unit: {unit!r}")


def build_transform(
    *,
    translate_x_pt: float = 0.0,
    translate_y_pt: float = 0.0,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    shear_x: float = 0.0,
    shear_y: float = 0.0,
) -> dict[str, Any]:
    """Build an AffineTransform dict (in PT units) for the Slides API."""
    return {
        "scaleX": scale_x,
        "scaleY": scale_y,
        "shearX": shear_x,
        "shearY": shear_y,
        "translateX": translate_x_pt,
        "translateY": translate_y_pt,
        "unit": "PT",
    }


def bounding_box(
    size: dict[str, Any] | None, transform: dict[str, Any] | None
) -> dict[str, float] | None:
    """Compute an element's visual bounding box in points.

    Combines the intrinsic ``size`` with the element ``transform``:

        width'  = scaleX * width  + shearX * height
        height' = scaleY * height + shearY * width

    with the upper-left corner at (translateX, translateY).

    Args:
        size: A Slides ``Size`` ({width: Dimension, height: Dimension}).
        transform: A Slides ``AffineTransform``.

    Returns:
        ``{x, y, width, height}`` in points, or None if size/transform missing.
    """
    if not size or not transform:
        return None

    width = to_pt(size.get("width")) or 0.0
    height = to_pt(size.get("height")) or 0.0

    scale_x = transform.get("scaleX", 1.0)
    scale_y = transform.get("scaleY", 1.0)
    shear_x = transform.get("shearX", 0.0)
    shear_y = transform.get("shearY", 0.0)

    # translate{X,Y} are themselves expressed in the transform's unit.
    unit = transform.get("unit", "EMU")
    tx = transform.get("translateX", 0.0)
    ty = transform.get("translateY", 0.0)
    if unit == "EMU":
        tx = emu_to_pt(tx)
        ty = emu_to_pt(ty)

    visual_width = scale_x * width + shear_x * height
    visual_height = scale_y * height + shear_y * width

    return {
        "x": round(tx, 3),
        "y": round(ty, 3),
        "width": round(visual_width, 3),
        "height": round(visual_height, 3),
    }
