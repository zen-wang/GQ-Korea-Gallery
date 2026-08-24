"""Download source images and re-encode them for the gallery.

Two outputs per source image, both WebP: a `full` capped at MAX_EDGE for the
lightbox and a `thumb` capped at THUMB_EDGE for the grid. PHASE0_AMENDMENTS §B.4
has the grid render thumbs exclusively, so the thumb is not an optimisation —
it is what almost every page view actually downloads.

`content_hash` is the sha256 of the *source* bytes, never of what we encode.
That is the identity the images table's one unique constraint arbitrates, and
pinning it to the source means changing FULL_QUALITY or WEBP_METHOD later
re-encodes rows in place instead of orphaning every object in the bucket.

Everything here is decode-side hostile territory: the bytes come from a third
party's CDN and may be HTML, half a download, or a header claiming a gigapixel.
Each of those raises ImageError with the URL and byte count, so the pipeline can
count and skip one bad image rather than losing the run.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PIL import Image, ImageOps, UnidentifiedImageError

if TYPE_CHECKING:  # Annotation only: the optimizer calls get_bytes and nothing
    # else, so tests inject a stub and this module stays importable on its own.
    from gallery_scraper.core.http import PoliteClient

MAX_EDGE = 1600  # PLAN.md §Risks: the free-tier storage budget.
THUMB_EDGE = 600  # PHASE0_AMENDMENTS §B.4: the grid renders thumbs only.

# q82 is the knee of the WebP curve for editorial photography — artefacts stay
# invisible against skin and fabric while the file lands near a third of the JPEG.
FULL_QUALITY = 82
# Thumbs display around 300 CSS px, where compression artefacts are unresolvable
# and bytes are the only thing the visitor notices.
THUMB_QUALITY = 70
# The slowest, densest encoder setting: each image is encoded once and served
# forever, so CPU here is the cheapest thing we can spend on the storage cap.
WEBP_METHOD = 6

# ~240MB of decoded RGB. Well past any editorial photograph, and short enough to
# refuse a bomb before it is allocated rather than after.
MAX_PIXELS = 80_000_000

# Alpha is flattened rather than kept: WebP carries it fine, but the grid
# composites on a light canvas where a transparent region reads as a hole.
FLATTEN_BACKGROUND = (255, 255, 255)
_ALPHA_MODES = frozenset({"RGBA", "LA", "La", "PA"})

_MEMORY_ORIGIN = "<in-memory source>"


class ImageError(RuntimeError):
    """A source image could not be decoded, normalised or re-encoded."""


@dataclass(frozen=True)
class OptimizedImage:
    full: bytes  # WebP, long edge <= MAX_EDGE
    thumb: bytes  # WebP, long edge <= THUMB_EDGE
    width: int  # dimensions of `full`, because that is what images.width stores
    height: int
    content_hash: str  # sha256 hex of the original bytes, per the shared contract


def optimize(source_bytes: bytes) -> OptimizedImage:
    """Re-encode one source image. Pure: no network, no filesystem."""
    return _optimize(source_bytes, _MEMORY_ORIGIN)


def download_and_optimize(url: str, client: PoliteClient) -> OptimizedImage:
    """Fetch one image through `client` and optimize it.

    Transport failures propagate as the client's own HttpError. Only unusable
    bytes become ImageError — the pipeline tallies the two separately, and an
    outage misfiled as a bad image would look like a content problem.
    """
    target = url.strip()
    if not target:
        raise ImageError("refusing to fetch an image with a blank URL")
    return _optimize(client.get_bytes(target), target)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

def _optimize(source_bytes: bytes, origin: str) -> OptimizedImage:
    context = f"{origin} ({len(source_bytes)} bytes)"
    if not source_bytes:
        raise ImageError(f"empty response body: {context}")

    opened = _open(source_bytes, context)
    try:
        normalized = _normalize(opened, context)
    finally:
        # The decoder holds the BytesIO and, for animations, a frame buffer.
        opened.close()

    full = _fit(normalized, MAX_EDGE)
    thumb = _fit(normalized, THUMB_EDGE)  # from the same source as `full`, so
    # the thumb never compounds the full's resampling artefacts.

    return OptimizedImage(
        full=_encode(full, FULL_QUALITY, context),
        thumb=_encode(thumb, THUMB_QUALITY, context),
        width=full.width,
        height=full.height,
        content_hash=hashlib.sha256(source_bytes).hexdigest(),
    )


def _open(source_bytes: bytes, context: str) -> Image.Image:
    """Parse the header only, then refuse implausible pixel counts.

    Image.open reads dimensions without decoding pixels, which is the whole
    point: the bomb guard gets to run before any allocation happens.
    """
    try:
        with warnings.catch_warnings():
            # Pillow merely warns past its own threshold. We refuse below, so
            # its warning is redundant noise on a path we already handle.
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(source_bytes))
    except Image.DecompressionBombError as exc:
        # Past twice Pillow's limit it refuses before returning a header at all.
        raise ImageError(f"decompression bomb refused by the decoder: {context}") from exc
    except UnidentifiedImageError as exc:
        raise ImageError(f"not a recognisable image: {context}") from exc
    except (OSError, ValueError) as exc:
        raise ImageError(f"unreadable image header: {context}") from exc

    pixels = image.width * image.height
    if pixels > MAX_PIXELS:
        image.close()
        raise ImageError(
            f"image declares {pixels} pixels, over the {MAX_PIXELS} limit: {context}"
        )
    return image


def _normalize(image: Image.Image, context: str) -> Image.Image:
    """Return a new RGB image, upright, single-frame, fully decoded."""
    try:
        if getattr(image, "n_frames", 1) > 1:
            # Explicitly frame one: an animation has no meaningful "the" image,
            # and the last frame of a GIF is often a blank or a logo card.
            image.seek(0)
        # exif_transpose returns a copy and forces the decode, so a truncated
        # file fails here rather than three functions later inside the encoder.
        upright = ImageOps.exif_transpose(image)
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImageError(f"truncated or corrupt image data: {context}") from exc

    try:
        return _to_rgb(upright)
    except (OSError, ValueError) as exc:
        raise ImageError(f"cannot convert {upright.mode} to RGB: {context}") from exc


def _to_rgb(image: Image.Image) -> Image.Image:
    """Flatten to RGB, compositing any alpha onto FLATTEN_BACKGROUND."""
    if image.mode == "RGB":
        return image
    if image.mode in _ALPHA_MODES or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, FLATTEN_BACKGROUND)
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def _fit(image: Image.Image, max_edge: int) -> Image.Image:
    """Scale down to `max_edge` on the long side, preserving the aspect ratio.

    Never upscales: a source already under the cap keeps its dimensions and only
    its encoding changes. The one-pixel floor matters because images.width and
    images.height carry CHECK (> 0), and a 5000x1 banner would otherwise round
    its short edge to zero and take the whole insert down with it.
    """
    long_edge = max(image.width, image.height)
    if long_edge <= max_edge:
        return image

    scale = max_edge / long_edge
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _encode(image: Image.Image, quality: int, context: str) -> bytes:
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="WEBP", quality=quality, method=WEBP_METHOD)
    except (OSError, ValueError) as exc:
        raise ImageError(f"WebP encoding failed at q{quality}: {context}") from exc
    return buffer.getvalue()
