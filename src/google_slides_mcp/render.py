"""Page rendering (PNG snapshots) and image diffing for pixel verification.

``presentations.pages.getThumbnail`` renders the *server-side* image of a slide —
the same pixels a viewer sees — and returns a ``contentUrl`` valid for ~30 minutes.
We fetch it immediately and hand the PNG bytes back to the model so it can verify
output is pixel-perfect. ``diff`` overlays two renders with Pillow to quantify any
mismatch.

getThumbnail is an *expensive* quota operation (300/min project, 60/min user), so
rendering is always explicit and per-page — there is deliberately no "render the
whole deck" call.
"""

from __future__ import annotations

import io
from typing import Any

from google.auth.transport.requests import AuthorizedSession
from PIL import Image as PILImage
from PIL import ImageChops

from .auth import load_credentials
from .client import Services, execute

# Map friendly sizes to the API enum. LARGE≈1600px, MEDIUM≈800px, SMALL≈200px wide.
THUMBNAIL_SIZES = {"LARGE": "LARGE", "MEDIUM": "MEDIUM", "SMALL": "SMALL"}


def _fetch_png(content_url: str) -> bytes:
    """Download a thumbnail content URL using the authorized session."""
    session = AuthorizedSession(load_credentials())
    response = session.get(content_url)
    response.raise_for_status()
    return response.content


def render(
    services: Services,
    presentation_id: str,
    page_id: str,
    size: str = "MEDIUM",
    mime_type: str = "PNG",
) -> dict[str, Any]:
    """Render a single page to PNG bytes (1 thumbnail call + 1 download).

    Returns:
        ``{"png": bytes, "width": int, "height": int}``.
    """
    size = size.upper()
    if size not in THUMBNAIL_SIZES:
        raise ValueError(f"size must be one of {sorted(THUMBNAIL_SIZES)}, got {size!r}")

    thumbnail = execute(
        services.slides.presentations()
        .pages()
        .getThumbnail(
            presentationId=presentation_id,
            pageObjectId=page_id,
            thumbnailProperties_mimeType=mime_type,
            thumbnailProperties_thumbnailSize=THUMBNAIL_SIZES[size],
        )
    )
    png = _fetch_png(thumbnail["contentUrl"])
    return {
        "png": png,
        "width": thumbnail.get("width"),
        "height": thumbnail.get("height"),
    }


def diff(png_a: bytes, png_b: bytes) -> dict[str, Any]:
    """Compare two PNGs and quantify their visual difference.

    The second image is resized to match the first if dimensions differ. Returns a
    normalized ``mismatch`` ratio in [0, 1] (mean absolute per-channel difference)
    and a PNG visualization of the differing regions.

    Returns:
        ``{"mismatch": float, "identical": bool, "diff_png": bytes}``.
    """
    image_a = PILImage.open(io.BytesIO(png_a)).convert("RGB")
    image_b = PILImage.open(io.BytesIO(png_b)).convert("RGB")

    if image_a.size != image_b.size:
        image_b = image_b.resize(image_a.size)

    difference = ImageChops.difference(image_a, image_b)

    # Mean absolute difference across all pixels/channels, normalized to [0, 1].
    histogram = difference.histogram()
    total = 0
    count = 0
    for channel in range(3):
        channel_hist = histogram[channel * 256 : (channel + 1) * 256]
        for value, freq in enumerate(channel_hist):
            total += value * freq
            count += freq
    mismatch = (total / count / 255.0) if count else 0.0

    buffer = io.BytesIO()
    # Amplify the diff so small deltas are visible.
    PILImage.eval(difference, lambda px: min(255, px * 8)).save(buffer, format="PNG")
    return {
        "mismatch": round(mismatch, 6),
        "identical": difference.getbbox() is None,
        "diff_png": buffer.getvalue(),
    }
