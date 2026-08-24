"""Image uploads to Supabase Storage.

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

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # supabase stays out of the import path at runtime
    from supabase import Client

BUCKET = "gallery"

# Objects are content-addressed, so a given path keeps serving the same picture
# and the CDN may hold it rather than revalidate once per tile. Not `immutable`,
# though: content_hash is taken over the *source* bytes, so re-encoding at new
# settings would replace the object behind an unchanged path, and that campaign
# would need a purge.
CACHE_CONTROL_SECONDS = 31_536_000  # one year


class StorageError(RuntimeError):
    """An upload did not produce a usable public URL."""


def object_path(article_id: str, content_hash: str, *, thumb: bool = False) -> str:
    """Bucket-relative path for one image."""
    suffix = "_t" if thumb else ""
    return f"{article_id}/{content_hash[:16]}{suffix}.webp"


def upload(client: Client, path: str, body: bytes, *, content_type: str = "image/webp") -> str:
    """Upload one object and return its public URL.

    Uploads run as service_role, which bypasses the deny-all RLS on
    storage.objects — that is the only writer by design.

    `upsert` is on because re-scraping the same article re-derives the same
    paths: an already-present object is the ordinary case on the second run,
    not a failure, and a plain POST would answer 409 for every image.

    Raises StorageError rather than returning a partial result. A caller that
    stored an empty public_url would violate the NOT NULL, and a caller that
    stored a URL for bytes that never landed would publish a broken tile.
    """
    if not path:
        raise StorageError("refusing to upload to an empty object path")
    if not body:
        raise StorageError(f"refusing to upload zero bytes to {BUCKET}/{path}")

    bucket = client.storage.from_(BUCKET)
    try:
        # A fresh dict per call: storage3 pops keys out of what it is handed.
        bucket.upload(
            path=path,
            file=body,
            file_options={
                "content-type": content_type,
                "cache-control": str(CACHE_CONTROL_SECONDS),
                "upsert": "true",
            },
        )
        public_url = bucket.get_public_url(path)
    except Exception as exc:  # any transport/API failure, re-raised with context
        raise StorageError(f"upload to {BUCKET}/{path} failed") from exc

    if not public_url:
        raise StorageError(f"no public URL returned for {BUCKET}/{path}")
    return public_url
