"""Tests for the image optimizer.

Every source image is generated in memory with Pillow rather than committed as a
binary fixture: the properties under test are geometric (edges, aspect ratio,
orientation, mode), so a synthesized image proves them exactly as well as a real
one, and the repo stays free of GQ Korea's photography.

The DB constraints these tests defend are worth naming, because they are what
turns a cosmetic bug into a failed run:
  - images.width/height carry CHECK (> 0), so no rounding may produce a zero edge
  - width/height describe `full`, so orientation must be resolved before measuring
  - images.content_hash arbitrates the one unique constraint, so it must be a
    property of the *source* bytes and stable across re-scrapes
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import struct
import zlib

import pytest
from PIL import Image

from gallery_scraper import images as images_module
from gallery_scraper.images import (
    FULL_QUALITY,
    MAX_EDGE,
    MAX_PIXELS,
    THUMB_EDGE,
    THUMB_QUALITY,
    WEBP_METHOD,
    ImageError,
    OptimizedImage,
    download_and_optimize,
    optimize,
)


# --------------------------------------------------------------------------
# Source builders
# --------------------------------------------------------------------------

def _encoded(image: Image.Image, fmt: str, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def _png(size: tuple[int, int], color: tuple[int, int, int] = (20, 90, 200)) -> bytes:
    return _encoded(Image.new("RGB", size, color), "PNG")


def _detailed(size: tuple[int, int]) -> bytes:
    """A PNG whose every pixel differs from its neighbours.

    Flat colour compresses to the same handful of bytes at any quality, so a
    test that needs q82 and q70 to be distinguishable needs detail the encoder
    has to make choices about. The pattern is arithmetic, not random, so the
    encoded bytes are reproducible run to run.
    """
    width, height = size
    pixels = bytes((x * 7 + y * 13 + channel * 53) % 256
                   for y in range(height) for x in range(width) for channel in range(3))
    return _encoded(Image.frombytes("RGB", size, pixels), "PNG")


def _decoded(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload))
    )


def _png_declaring(width: int, height: int) -> bytes:
    """A PNG whose IHDR claims `width`x`height` but carries no pixel data.

    This is the shape of a decompression bomb: a few dozen bytes on the wire
    that ask the decoder for gigabytes of RAM. The guard has to fire on the
    header alone, which is exactly what a payload-free file proves.
    """
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", b"")
        + _png_chunk(b"IEND", b"")
    )


class _FakeClient:
    """Stands in for PoliteClient. The optimizer only ever calls get_bytes."""

    def __init__(self, body: bytes | Exception) -> None:
        self.body = body
        self.requested: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.requested.append(url)
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class _TransportError(RuntimeError):
    """Stands in for core.http.HttpError, which is also a RuntimeError."""


# --------------------------------------------------------------------------
# Budget constants
# --------------------------------------------------------------------------

def test_edge_budgets_match_the_storage_plan():
    # PLAN.md §Risks caps the long edge at 1600; PHASE0_AMENDMENTS §B.4 renders
    # the grid from thumbs alone. Changing either changes the storage bill.
    assert MAX_EDGE == 1600
    assert THUMB_EDGE == 600


def test_encoder_budgets_match_the_storage_plan():
    # The other half of the storage bill: q82 is the knee of the WebP curve for
    # editorial photography, thumbs are the file almost every page view pulls,
    # and method 6 buys density with CPU we only spend once per image.
    assert FULL_QUALITY == 82
    assert THUMB_QUALITY == 70
    assert THUMB_QUALITY < FULL_QUALITY
    assert WEBP_METHOD == 6


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def test_oversized_landscape_is_capped_at_max_edge():
    result = optimize(_png((3000, 2000)))
    assert (result.width, result.height) == (MAX_EDGE, 1067)
    assert _decoded(result.full).size == (MAX_EDGE, 1067)


def test_oversized_portrait_is_capped_at_max_edge():
    result = optimize(_png((1200, 2400)))
    assert (result.width, result.height) == (800, MAX_EDGE)
    assert _decoded(result.full).size == (800, MAX_EDGE)


def test_small_image_is_never_upscaled():
    # A 320px source stays 320px: upscaling invents detail and costs bytes.
    source = _png((320, 200))
    result = optimize(source)
    assert (result.width, result.height) == (320, 200)
    assert _decoded(result.thumb).size == (320, 200)
    assert result.full != source  # the encoding still changed


def test_aspect_ratio_survives_the_downscale():
    result = optimize(_png((3000, 2000)))
    expected = result.width * 2000 / 3000
    assert abs(result.height - expected) <= 1


def test_an_extreme_aspect_ratio_never_rounds_an_edge_to_zero():
    # 1px tall at 5000 wide scales to 0.32px: the DB's CHECK (height > 0) would
    # reject the row, so the floor of one pixel is load-bearing.
    result = optimize(_png((5000, 1)))
    assert (result.width, result.height) == (MAX_EDGE, 1)
    assert _decoded(result.thumb).size == (THUMB_EDGE, 1)


def test_an_extreme_portrait_never_rounds_an_edge_to_zero():
    # The same floor from the other side: 1px wide at 5000 tall. Landscape and
    # portrait round through different branches, and CHECK (width > 0) is just
    # as fatal as CHECK (height > 0).
    result = optimize(_png((1, 5000)))
    assert (result.width, result.height) == (1, MAX_EDGE)
    assert _decoded(result.thumb).size == (1, THUMB_EDGE)


def test_thumb_geometry_is_derived_from_the_source_not_from_full():
    # 1601x989 rounds to 988 on the way to `full`; carrying that rounded height
    # into the thumb would compound the error into 370 rather than the 371 the
    # source asks for. The thumb has to be its own reduction of the original —
    # which is also what keeps it from inheriting `full`'s resampling.
    result = optimize(_png((1601, 989)))
    assert (result.width, result.height) == (MAX_EDGE, 988)
    assert _decoded(result.thumb).size == (THUMB_EDGE, 371)


def test_thumb_is_strictly_smaller_than_full():
    result = optimize(_png((3000, 2000)))
    thumb = _decoded(result.thumb)
    assert max(thumb.size) == THUMB_EDGE
    assert max(thumb.size) < max(result.width, result.height)


def test_both_outputs_are_webp():
    result = optimize(_png((800, 600)))
    assert _decoded(result.full).format == "WEBP"
    assert _decoded(result.thumb).format == "WEBP"


def test_each_output_is_encoded_at_its_own_quality_budget():
    # `full` is what the lightbox shows and `thumb` is what the grid pulls in
    # bulk; swapping their budgets would either blur the lightbox or triple the
    # bytes on the page that matters. Re-encoding the same pixels here is the
    # only way to see which budget each output actually got.
    source = _detailed((900, 600))
    upright = _decoded(source).convert("RGB")
    reference_full = _encoded(upright, "WEBP", quality=FULL_QUALITY, method=WEBP_METHOD)
    reference_thumb = _encoded(
        upright.resize((THUMB_EDGE, 400), Image.Resampling.LANCZOS),
        "WEBP",
        quality=THUMB_QUALITY,
        method=WEBP_METHOD,
    )
    # Guards the two assertions below: on flat colour the two budgets collapse
    # to the same bytes and this test would prove nothing.
    assert reference_full != _encoded(upright, "WEBP", quality=THUMB_QUALITY,
                                      method=WEBP_METHOD)

    result = optimize(source)

    assert result.full == reference_full
    assert result.thumb == reference_thumb


# --------------------------------------------------------------------------
# Orientation and colour modes
# --------------------------------------------------------------------------

def test_exif_orientation_is_applied_before_measuring():
    # Orientation 6 means "the camera was on its side": the stored raster is
    # 800x400 but it must be displayed 400x800. Measuring before transposing
    # would store dimensions that contradict what the masonry renders.
    exif = Image.Exif()
    exif[0x0112] = 6
    source = _encoded(Image.new("RGB", (800, 400), (10, 120, 200)), "JPEG", exif=exif)

    result = optimize(source)

    assert (result.width, result.height) == (400, 800)
    assert _decoded(result.full).size == (400, 800)


def test_cmyk_source_becomes_an_rgb_webp():
    # Print-origin JPEGs arrive as CMYK, which WebP cannot encode at all.
    source = _encoded(Image.new("CMYK", (120, 90), (0, 200, 200, 10)), "JPEG")

    result = optimize(source)

    full = _decoded(result.full)
    assert full.mode == "RGB"
    assert full.size == (120, 90)


def test_transparent_source_is_flattened_onto_white():
    # The grid composites on a light canvas, so surviving transparency reads as
    # a hole rather than as design.
    source = _encoded(Image.new("RGBA", (100, 100), (0, 0, 0, 0)), "PNG")

    result = optimize(source)

    full = _decoded(result.full)
    assert full.mode == "RGB"
    assert all(channel >= 245 for channel in full.getpixel((50, 50)))


def test_greyscale_alpha_source_survives():
    source = _encoded(Image.new("LA", (80, 60), (128, 255)), "PNG")

    result = optimize(source)

    assert _decoded(result.full).mode == "RGB"
    assert (result.width, result.height) == (80, 60)


def test_transparent_greyscale_is_composited_not_merely_converted():
    # LA carries alpha too, and converting it straight to RGB drops the mask
    # instead of honouring it: a transparent region would arrive as its own
    # dark grey rather than as the canvas behind it.
    source = _encoded(Image.new("LA", (80, 60), (30, 0)), "PNG")

    result = optimize(source)

    assert all(channel >= 245 for channel in _decoded(result.full).getpixel((40, 30)))


def test_palette_transparency_is_flattened_onto_white():
    # The web's other transparent image: an 8-bit PNG whose tRNS chunk marks one
    # palette index see-through. Its mode is "P", not RGBA, so it only reaches
    # the compositing path if transparency is looked for in info as well.
    palette = Image.new("P", (60, 40))
    palette.putpalette([220, 20, 20] * 256)
    source = _encoded(palette, "PNG", transparency=0)

    result = optimize(source)

    full = _decoded(result.full)
    assert full.mode == "RGB"
    assert all(channel >= 245 for channel in full.getpixel((30, 20)))


def test_palette_source_survives():
    palette = Image.new("RGB", (120, 90), (200, 40, 60)).convert("P", palette=Image.ADAPTIVE)
    source = _encoded(palette, "PNG")

    result = optimize(source)

    full = _decoded(result.full)
    assert full.mode == "RGB"
    assert full.size == (120, 90)


def test_animated_gif_takes_its_first_frame():
    red = Image.new("P", (200, 100))
    red.putpalette([220, 20, 20] * 256)
    blue = Image.new("P", (200, 100))
    blue.putpalette([20, 20, 220] * 256)
    source = _encoded(red, "GIF", save_all=True, append_images=[blue], duration=100)

    result = optimize(source)

    full = _decoded(result.full).convert("RGB")
    assert full.size == (200, 100)
    red_channel, _, blue_channel = full.getpixel((5, 5))
    assert red_channel > blue_channel  # frame one, not the last frame


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

def test_content_hash_is_the_source_digest_not_the_output_digest():
    # The shared contract: hashing the source keeps rows matching across encoder
    # changes, so a re-scrape updates instead of re-uploading.
    source = _png((640, 480))
    result = optimize(source)
    assert result.content_hash == hashlib.sha256(source).hexdigest()
    assert result.content_hash != hashlib.sha256(result.full).hexdigest()
    assert result.content_hash != hashlib.sha256(result.thumb).hexdigest()


def test_identical_bytes_hash_identically_across_calls():
    source = _png((640, 480))
    assert optimize(source).content_hash == optimize(bytes(source)).content_hash


def test_different_bytes_hash_differently():
    assert optimize(_png((640, 480))).content_hash != optimize(_png((641, 480))).content_hash


def test_optimized_image_is_immutable():
    result = optimize(_png((64, 64)))
    assert isinstance(result, OptimizedImage)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.width = 1  # type: ignore[misc]


# --------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------

def test_garbage_bytes_raise_image_error_with_size_context():
    payload = b"<html>404 not found</html>"
    with pytest.raises(ImageError) as caught:
        optimize(payload)
    assert str(len(payload)) in str(caught.value)


def test_empty_body_raises_image_error():
    # A zero-byte body would also fail inside the decoder, so the message is
    # what proves it was refused up front, before Pillow was asked anything.
    with pytest.raises(ImageError) as caught:
        optimize(b"")
    assert "empty response body" in str(caught.value)


def test_truncated_image_raises_image_error():
    # A cut-off download decodes its header fine and only fails on the pixels,
    # so the failure has to be forced eagerly rather than left to the encoder.
    source = _png((400, 300), color=(7, 200, 90))
    with pytest.raises(ImageError):
        optimize(source[: len(source) // 2])


def test_declared_huge_image_is_refused_before_decoding():
    # 96M pixels is ~288MB of RGB. Refuse on the header rather than allocating.
    #
    # The message is the assertion: a payload-free file fails later anyway, as
    # "truncated or corrupt image data", so checking only the exception type
    # would pass with the guard deleted. Naming the declared pixel count proves
    # the header check is what refused it.
    with pytest.raises(ImageError) as caught:
        optimize(_png_declaring(12000, 8000))
    message = str(caught.value)
    assert f"{12000 * 8000} pixels" in message
    assert str(MAX_PIXELS) in message


def test_a_pixel_count_over_the_limit_is_refused_even_when_the_file_would_decode(
    monkeypatch,
):
    # The other half of the same proof, from the opposite direction: a real,
    # wholly valid PNG that only the guard can object to. Building an actual
    # 80-megapixel source would cost the RAM this limit exists to refuse, so the
    # limit comes down to meet a small one instead.
    source = _png((100, 100))
    monkeypatch.setattr(images_module, "MAX_PIXELS", 1_000)

    with pytest.raises(ImageError) as caught:
        optimize(source)
    assert f"{100 * 100} pixels" in str(caught.value)

    monkeypatch.undo()
    assert optimize(source).width == 100  # the very same bytes, once the cap lifts


def test_pillows_own_bomb_refusal_is_reported_as_image_error():
    # Past twice Pillow's own limit it raises before we ever see the header;
    # that must still reach the caller as the one error type it counts.
    with pytest.raises(ImageError):
        optimize(_png_declaring(40000, 40000))


# --------------------------------------------------------------------------
# Error translation
#
# The pipeline catches exactly one type per image and moves on. A raw OSError
# escaping from anywhere in Pillow would fall through that handler and take the
# run down, so each stage is forced to fail once and checked for the wrapper.
# --------------------------------------------------------------------------

def test_a_failing_header_read_surfaces_as_image_error(monkeypatch):
    def _explode(*_args, **_kwargs):
        raise OSError("cannot seek")

    monkeypatch.setattr(Image, "open", _explode)
    with pytest.raises(ImageError):
        optimize(b"\x89PNG\r\n\x1a\n")


def test_a_failing_mode_conversion_surfaces_as_image_error(monkeypatch):
    source = _encoded(Image.new("CMYK", (60, 40), (0, 0, 0, 0)), "JPEG")

    def _explode(*_args, **_kwargs):
        raise ValueError("unsupported conversion")

    monkeypatch.setattr(Image.Image, "convert", _explode)
    with pytest.raises(ImageError):
        optimize(source)


def test_a_failing_encode_surfaces_as_image_error(monkeypatch):
    source = _png((64, 64))  # built before the encoder is broken

    def _explode(*_args, **_kwargs):
        raise OSError("encoder unavailable")

    monkeypatch.setattr(Image.Image, "save", _explode)
    with pytest.raises(ImageError):
        optimize(source)


# --------------------------------------------------------------------------
# download_and_optimize
# --------------------------------------------------------------------------

def test_download_and_optimize_fetches_once_and_optimizes_the_body():
    source = _png((3000, 2000))
    client = _FakeClient(source)

    result = download_and_optimize("https://example.test/a.jpg", client)

    assert client.requested == ["https://example.test/a.jpg"]
    assert result.content_hash == hashlib.sha256(source).hexdigest()
    assert (result.width, result.height) == (MAX_EDGE, 1067)


def test_download_and_optimize_names_the_url_in_its_error():
    client = _FakeClient(b"not an image")
    with pytest.raises(ImageError) as caught:
        download_and_optimize("https://example.test/broken.jpg", client)
    assert "https://example.test/broken.jpg" in str(caught.value)


def test_transport_errors_are_not_reported_as_image_errors():
    # The pipeline counts network failures separately from unusable images, and
    # ImageError is itself a RuntimeError — so relabelling one as the other
    # would quietly hide an outage behind a "bad image" tally.
    client = _FakeClient(_TransportError("boom"))
    with pytest.raises(_TransportError):
        download_and_optimize("https://example.test/a.jpg", client)


def test_blank_url_is_rejected_without_calling_the_client():
    client = _FakeClient(_png((64, 64)))
    with pytest.raises(ImageError):
        download_and_optimize("   ", client)
    assert client.requested == []
