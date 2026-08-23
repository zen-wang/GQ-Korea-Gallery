"""Download and optimize article images (resize <=1600px, WebP + thumbnail). Phase 3."""

from __future__ import annotations


def download_and_optimize(source_image_url: str) -> tuple[bytes, bytes]:
    """Return (webp_full, webp_thumb) for one source image."""
    raise NotImplementedError("Phase 3")
