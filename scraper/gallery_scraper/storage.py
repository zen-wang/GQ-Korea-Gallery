"""Image uploads to Supabase Storage. Implemented in Phase 3.

Supabase Storage rather than Cloudflare R2: R2 needs a payment method on file
even inside its free tier. See PHASE0_AMENDMENTS §E.

The bucket ("gallery") is created by migration 20260823222834 and is public
with unguessable paths, so a stored object's URL is stable and cacheable and
`<img src>` works without minting anything per request.

Paths are `<article_id>/<content_hash[:16]>.webp`, with `_t` appended for the
thumbnail. The article UUID prefix is what makes them unguessable; the hash
suffix means identical bytes resolve to one object.

supabase is imported lazily inside functions so the package imports without it.
"""

from __future__ import annotations

BUCKET = "gallery"


def object_path(article_id: str, content_hash: str, *, thumb: bool = False) -> str:
    """Bucket-relative path for one image."""
    suffix = "_t" if thumb else ""
    return f"{article_id}/{content_hash[:16]}{suffix}.webp"


def upload(path: str, body: bytes, content_type: str = "image/webp") -> str:
    """Upload one object and return its public URL.

    Uploads run as service_role, which bypasses the deny-all RLS on
    storage.objects — that is the only writer by design.
    """
    raise NotImplementedError("Phase 3")
