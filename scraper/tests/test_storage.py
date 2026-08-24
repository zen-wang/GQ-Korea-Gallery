"""Storage tests. No network and no live bucket: a fake supabase client records
what would have been sent.

The fake reproduces the one server behaviour that decides this module's shape —
a POST to a path that already exists is refused unless `x-upsert` is set. Every
re-scrape re-uploads paths it wrote last time, so an upload that is not
idempotent turns the second run of the pipeline into a wall of failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pytest

from gallery_scraper.storage import BUCKET, StorageError, object_path, upload

ARTICLE_ID = "11111111-2222-3333-4444-555555555555"
CONTENT_HASH = "a1b2c3d4e5f60718" + "0" * 48  # sha256 hex of the *source* bytes
BODY = b"not-really-webp-but-bytes-all-the-same"


class FakeStorageApiError(RuntimeError):
    """Stands in for storage3's StorageApiError.

    Deliberately not the real class: upload() has to survive any client-side
    failure, not just the one exception type this SDK version happens to raise.
    """


@dataclass(frozen=True)
class Upload:
    path: str
    body: bytes
    options: Mapping[str, str]


class FakeBucket:
    """Enough of storage3's SyncBucketProxy to record and to say no."""

    URL_PREFIX = "https://project.supabase.test/storage/v1/object/public"

    def __init__(self, bucket_id: str) -> None:
        self.id = bucket_id
        self.objects: dict[str, bytes] = {}
        self.uploads: list[Upload] = []
        self.raise_on_upload: Exception | None = None
        self.public_url_override: str | None = None

    def upload(self, path=None, file=None, file_options=None):
        options = dict(file_options or {})
        self.uploads.append(Upload(path=path, body=file, options=options))
        if self.raise_on_upload is not None:
            raise self.raise_on_upload
        if path in self.objects and str(options.get("upsert", "")).lower() != "true":
            raise FakeStorageApiError("Duplicate")
        self.objects[path] = file
        return {"path": path}

    def get_public_url(self, path: str) -> str:
        if self.public_url_override is not None:
            return self.public_url_override
        return f"{self.URL_PREFIX}/{self.id}/{path}"


class FakeStorage:
    def __init__(self) -> None:
        self.requested: list[str] = []
        self.buckets: dict[str, FakeBucket] = {}

    def from_(self, bucket_id: str) -> FakeBucket:
        self.requested.append(bucket_id)
        return self.buckets.setdefault(bucket_id, FakeBucket(bucket_id))


class FakeClient:
    def __init__(self) -> None:
        self.storage = FakeStorage()


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def bucket_of(client: FakeClient) -> FakeBucket:
    return client.storage.buckets[BUCKET]


# --------------------------------------------------------------------------
# object_path — unchanged from Phase 0, pinned so a refactor cannot drift it
# --------------------------------------------------------------------------

def test_object_path_is_article_scoped_and_hash_named():
    assert object_path(ARTICLE_ID, CONTENT_HASH) == f"{ARTICLE_ID}/a1b2c3d4e5f60718.webp"


def test_thumbnail_path_differs_only_by_suffix():
    assert object_path(ARTICLE_ID, CONTENT_HASH, thumb=True) == (
        f"{ARTICLE_ID}/a1b2c3d4e5f60718_t.webp"
    )


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------

def test_upload_writes_to_the_gallery_bucket(client):
    upload(client, object_path(ARTICLE_ID, CONTENT_HASH), BODY)
    assert client.storage.requested == [BUCKET]


def test_upload_returns_the_public_url_of_the_object(client):
    path = object_path(ARTICLE_ID, CONTENT_HASH)
    url = upload(client, path, BODY)
    assert url == f"{FakeBucket.URL_PREFIX}/{BUCKET}/{path}"


def test_upload_sends_the_bytes_it_was_given(client):
    path = object_path(ARTICLE_ID, CONTENT_HASH)
    upload(client, path, BODY)
    assert bucket_of(client).objects[path] == BODY


def test_upload_declares_the_content_type(client):
    upload(client, object_path(ARTICLE_ID, CONTENT_HASH), BODY)
    assert bucket_of(client).uploads[0].options["content-type"] == "image/webp"


def test_content_type_is_overridable(client):
    upload(client, "x/y.jpg", BODY, content_type="image/jpeg")
    assert bucket_of(client).uploads[0].options["content-type"] == "image/jpeg"


def test_upload_is_idempotent_across_re_scrapes(client):
    # The whole point: run two, path already there. Without x-upsert the API
    # answers 409 Duplicate, and the fake above answers exactly that.
    path = object_path(ARTICLE_ID, CONTENT_HASH)
    first = upload(client, path, BODY)
    second = upload(client, path, b"re-encoded-bytes")
    assert first == second
    assert bucket_of(client).objects[path] == b"re-encoded-bytes"


def test_upload_asks_for_upsert_explicitly(client):
    upload(client, object_path(ARTICLE_ID, CONTENT_HASH), BODY)
    assert bucket_of(client).uploads[0].options["upsert"] == "true"


def test_upload_sets_a_long_cache_control(client):
    # Paths are content-addressed and the bucket is public, so the CDN should
    # be allowed to keep an object rather than revalidate it per tile.
    upload(client, object_path(ARTICLE_ID, CONTENT_HASH), BODY)
    options = bucket_of(client).uploads[0].options
    assert int(options["cache-control"]) >= 86400


def test_api_failure_becomes_a_typed_error_and_never_a_url(client):
    path = object_path(ARTICLE_ID, CONTENT_HASH)
    client.storage.from_(BUCKET).raise_on_upload = FakeStorageApiError("bucket not found")
    with pytest.raises(StorageError) as excinfo:
        upload(client, path, BODY)
    assert path in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, FakeStorageApiError)


def test_an_empty_public_url_is_an_error_not_a_return_value(client):
    # public_url is NOT NULL and a row pointing at nothing is worse than a
    # failed scrape, so a blank URL has to stop the image here.
    client.storage.from_(BUCKET).public_url_override = ""
    with pytest.raises(StorageError):
        upload(client, object_path(ARTICLE_ID, CONTENT_HASH), BODY)


def test_empty_body_is_rejected_before_it_reaches_the_bucket(client):
    with pytest.raises(StorageError):
        upload(client, object_path(ARTICLE_ID, CONTENT_HASH), b"")
    assert client.storage.buckets.get(BUCKET) is None or not bucket_of(client).uploads


def test_empty_path_is_rejected(client):
    with pytest.raises(StorageError):
        upload(client, "", BODY)
