"""Cloudflare R2 uploads via boto3 (S3-compatible). Phase 3.

boto3 is imported lazily inside functions so the package imports without it.
"""

from __future__ import annotations


def upload(key: str, body: bytes, content_type: str = "image/webp") -> str:
    """Upload one object and return its public URL."""
    raise NotImplementedError("Phase 3")
