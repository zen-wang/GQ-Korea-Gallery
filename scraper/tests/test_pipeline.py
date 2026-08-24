"""Pipeline tests: every boundary is a fake.

The four modules the pipeline wires together carry their own tests. What is
under test here is the wiring — the order of the writes, which id each of them
is addressed to, what one failure costs, when an article is allowed to count as
finished, and what the exit code claims about the run — so the adapter, the
HTTP client and the Supabase sink are all stand-ins. No test opens a socket,
reads the environment, or constructs a Supabase client.

The optimizer is deliberately *not* faked: the bytes handed to it are real
(tiny) PNGs built with Pillow, because content_hash is what both the
duplicate-collapse and the object-path assertions turn on, and hashing a stub
would prove nothing. Tests that pin a derivative's path or bytes re-run the
same real optimizer over the same input rather than restating its output.

Content is synthetic throughout — this repo is public, so no GQ Korea title,
byline or URL appears here, the same policy the header of
tests/fixtures/article_pictorial.html records.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any

import pytest
from PIL import Image

from gallery_scraper import db, images, pipeline, storage
from gallery_scraper.core.adapter import (
    DEFAULT_MAX_PAGES,
    ArticleData,
    Credit,
    ImageRef,
    ListingEntry,
)
from gallery_scraper.core.http import DEFAULT_MIN_INTERVAL, HttpError
from gallery_scraper.db import ImageRow
from gallery_scraper.pipeline import (
    BYTES_PER_MIB,
    DRY_RUN_ARTICLE_ID,
    DRY_RUN_URL_PREFIX,
    FREE_TIER_BYTES,
    ArticleOutcome,
    ImageBatch,
    RunConfig,
    RunStats,
    Sink,
    build_sink,
    exit_code,
    main,
    parse_args,
    run,
    supabase_sink,
    without_writes,
)
from gallery_scraper.storage import StorageError

IMAGE_A = "https://img.example.test/a.jpg"
IMAGE_B = "https://img.example.test/b.jpg"
IMAGE_C = "https://img.example.test/c.jpg"

DEFAULT_HTML = "<html><body>synthetic article body</body></html>"

CDN_ORIGIN = "https://cdn.example.test"

# Not an image, and the realistic shape of the failure: a CDN answering an
# error page with a 200 is what actually reaches the optimizer.
NOT_AN_IMAGE = b"<!doctype html><html><body>404</body></html>"


def _png(color: tuple[int, int, int]) -> bytes:
    """A real, tiny PNG. The optimizer under test is the real one."""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


RED = _png((200, 30, 40))
BLUE = _png((20, 60, 200))


def listing_entry(number: int) -> ListingEntry:
    return ListingEntry(
        source_url=f"https://example.test/article-{number}/",
        category="pictorial",
        post_id=str(100 + number),
        title=f"Synthetic article {number}",
        published_date=dt.date(2026, 8, 21),
    )


def article_data(entry: ListingEntry, image_urls: Sequence[str] = ()) -> ArticleData:
    return ArticleData(
        source_url=entry.source_url,
        category=entry.category,
        title=entry.title,
        published_date=entry.published_date,
        author_name="Synthetic Editor",
        author_url="https://example.test/author/synthetic-editor/",
        credits=(
            Credit(role_raw="photographer", person_name="Synthetic Photographer"),
            Credit(role_raw="model", person_name="Synthetic Model", agency="Synthetic Agency"),
        ),
        images=tuple(
            ImageRef(source_url=url, position=index)
            for index, url in enumerate(image_urls, start=1)
        ),
    )


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------


def _yield(value: Any) -> Any:
    """Return a scripted reply, or raise it if that is what it is."""
    if isinstance(value, Exception):
        raise value
    return value


class FakeAdapter:
    """Stands in for GqKoreaAdapter.

    It mirrors the one discovery behaviour the pipeline depends on — an entry
    already in `seen` is skipped, and the walk carries on past it — so the
    incremental contract is observable here without the real listing endpoint.
    Skipping rather than stopping is the whole completion-marker protocol: an
    article left incomplete by an earlier run sits *behind* newer ones, and a
    walk that returned at the first known permalink could never reach it.
    """

    site = "fake"

    def __init__(
        self,
        entries: Sequence[ListingEntry],
        articles: Mapping[str, ArticleData | Exception] | None = None,
    ) -> None:
        self._entries = tuple(entries)
        self._articles = dict(articles or {})
        self.discovery_calls: list[dict[str, Any]] = []

    def discover_article_urls(
        self,
        *,
        max_pages: int = 200,
        seen: AbstractSet[str] = frozenset(),
    ) -> list[ListingEntry]:
        self.discovery_calls.append({"max_pages": max_pages, "seen": frozenset(seen)})
        return [entry for entry in self._entries if entry.source_url not in seen]

    def parse_article(self, html: str, source_url: str) -> ArticleData:
        return _yield(self._articles[source_url])


class FakeHttp:
    """The two PoliteClient methods the pipeline calls, plus its `with` block."""

    def __init__(
        self,
        *,
        pages: Mapping[str, str | Exception] | None = None,
        bodies: Mapping[str, bytes | Exception] | None = None,
    ) -> None:
        self._pages = dict(pages or {})
        self._bodies = dict(bodies or {})
        self.text_urls: list[str] = []
        self.byte_urls: list[str] = []
        self.closed = False

    def __enter__(self) -> "FakeHttp":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.closed = True
        return False

    def get_text(self, url: str) -> str:
        self.text_urls.append(url)
        return _yield(self._pages.get(url, DEFAULT_HTML))

    def get_bytes(self, url: str) -> bytes:
        self.byte_urls.append(url)
        return _yield(self._bodies[url])  # KeyError: the test forgot a fixture


def public_url_for(path: str) -> str:
    """What Recorder._upload hands back for one object path."""
    return f"{CDN_ORIGIN}/{path}"


def image_row(position: int) -> ImageRow:
    """One stored row, for the tallies that only ever count them."""
    content_hash = f"{position:064d}"
    full_path = storage.object_path("article-id-1", content_hash)
    thumb_path = storage.object_path("article-id-1", content_hash, thumb=True)
    return ImageRow(
        storage_path=full_path,
        public_url=public_url_for(full_path),
        thumb_url=public_url_for(thumb_path),
        width=1600,
        height=1067,
        position=position,
        source_image_url=IMAGE_A,
        content_hash=content_hash,
    )


def pipeline_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Only what this module emitted, in the order it emitted it."""
    return [record for record in caplog.records if record.name == "gallery_scraper.pipeline"]


class Recorder:
    """A Sink that records the whole conversation, in order and in full.

    Both halves matter. Order is what shows the objects reaching Storage before
    the rows that point at them, and the completion marker landing after the
    rows it vouches for. The arguments matter just as much: a recorder that
    logged only call names would let an article's images upload under a
    neighbour's id, or its credits be dropped, without a single test noticing.
    """

    def __init__(
        self,
        completed: AbstractSet[str] = frozenset(),
        *,
        fail_uploads: bool = False,
        fail_first_uploads: int = 0,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.article_ids: list[str] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.credit_calls: list[tuple[str, tuple[Credit, ...]]] = []
        self.image_batches: list[tuple[str, tuple[ImageRow, ...]]] = []
        self.completions: list[tuple[str, str]] = []
        self._completed = set(completed)
        self._fail_uploads = fail_uploads
        # A bucket that rejects the first n objects and then recovers — a
        # rate-limited or briefly unhappy Storage, and the one shape that tells
        # a retried image apart from one written off as already seen.
        self._fail_first_uploads = fail_first_uploads
        self._upload_calls = 0

    def sink(self) -> Sink:
        return Sink(
            completed_source_urls=self._completed_source_urls,
            upsert_article=self._upsert_article,
            replace_credits=self._replace_credits,
            upload=self._upload,
            upsert_images=self._upsert_images,
            mark_article_complete=self._mark_article_complete,
        )

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    @property
    def image_rows(self) -> list[ImageRow]:
        return [row for _article_id, rows in self.image_batches for row in rows]

    @property
    def upload_paths(self) -> list[str]:
        return [path for path, _body in self.uploads]

    def _completed_source_urls(self) -> set[str]:
        self.calls.append(("completed_source_urls", None))
        return set(self._completed)

    def _upsert_article(self, data: ArticleData) -> str:
        self.calls.append(("upsert_article", data.source_url))
        # Distinct per article on purpose: an id namespaces that article's
        # object paths, so a test can tell a real id from a constant.
        article_id = f"article-id-{len(self.article_ids) + 1}"
        self.article_ids.append(article_id)
        return article_id

    def _replace_credits(self, article_id: str, credits: Sequence[Credit]) -> None:
        self.calls.append(("replace_credits", article_id))
        self.credit_calls.append((article_id, tuple(credits)))

    def _upload(self, path: str, body: bytes) -> str:
        self.calls.append(("upload", path))
        self._upload_calls += 1
        if self._fail_uploads or self._upload_calls <= self._fail_first_uploads:
            raise StorageError(f"synthetic upload failure for {path}")
        self.uploads.append((path, body))
        return public_url_for(path)

    def _upsert_images(self, article_id: str, rows: Sequence[ImageRow]) -> None:
        self.calls.append(("upsert_images", article_id))
        self.image_batches.append((article_id, tuple(rows)))

    def _mark_article_complete(self, article_id: str, content_hash: str) -> None:
        self.calls.append(("mark_article_complete", article_id))
        self.completions.append((article_id, content_hash))


# --------------------------------------------------------------------------
# The completion-marker protocol
# --------------------------------------------------------------------------


def test_the_run_skips_completed_articles_and_still_reaches_the_ones_behind_them() -> None:
    first, second, third = listing_entry(1), listing_entry(2), listing_entry(3)
    entries = (first, second, third)
    adapter = FakeAdapter(entries, {e.source_url: article_data(e) for e in entries})
    http = FakeHttp()
    recorder = Recorder(completed={second.source_url})

    stats = run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    assert adapter.discovery_calls[0]["seen"] == frozenset({second.source_url})
    # Two, not one: the article *behind* the completed one is the whole reason
    # discovery skips instead of stopping. A capped or timed-out run leaves
    # exactly this shape of hole, and it has to heal.
    assert stats.articles_seen == 2
    assert stats.articles_new == 2
    assert http.text_urls == [first.source_url, third.source_url]


def test_an_article_whose_images_all_landed_is_marked_complete() -> None:
    entry = listing_entry(1)
    data = article_data(entry, [IMAGE_A])
    adapter = FakeAdapter([entry], {entry.source_url: data})
    http = FakeHttp(bodies={IMAGE_A: RED})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    # The marker is the digest of what was stored, not a flag: the next run
    # reads it to decide it may skip this article.
    assert recorder.completions == [(recorder.article_ids[0], db.article_content_hash(data))]


def test_the_marker_is_stamped_after_the_rows_it_vouches_for() -> None:
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A])})
    http = FakeHttp(bodies={IMAGE_A: RED})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    names = recorder.names
    # Marking first would claim completion for rows a crash could still lose.
    assert names.index("upsert_images") < names.index("mark_article_complete")
    assert names[-1] == "mark_article_complete"


def test_one_failed_image_leaves_the_whole_article_incomplete() -> None:
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A, IMAGE_B])})
    http = FakeHttp(bodies={IMAGE_A: HttpError("GET a.jpg: 403"), IMAGE_B: RED})
    recorder = Recorder()

    stats = run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    # Zero failures or nothing: a partially imaged article that counted as done
    # would sit imageless behind a green check for good.
    assert recorder.completions == []
    assert "mark_article_complete" not in recorder.names
    # What did survive is still written, so the next run only has the hole left.
    assert [row.source_image_url for row in recorder.image_rows] == [IMAGE_B]
    assert stats.articles_new == 1


def test_an_article_with_no_images_completes_normally() -> None:
    entry = listing_entry(1)
    data = article_data(entry)
    adapter = FakeAdapter([entry], {entry.source_url: data})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=FakeHttp(), sink=recorder.sink())

    # Zero images is zero image failures. Withholding the marker here would
    # make every text-only article a permanent re-fetch.
    assert recorder.completions == [(recorder.article_ids[0], db.article_content_hash(data))]


def test_a_failed_article_is_never_marked_complete() -> None:
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: ValueError("page layout changed")})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=FakeHttp(), sink=recorder.sink())

    assert recorder.completions == []


def test_max_articles_caps_one_run() -> None:
    entries = [listing_entry(n) for n in (1, 2, 3)]
    adapter = FakeAdapter(entries, {e.source_url: article_data(e) for e in entries})
    http = FakeHttp()
    recorder = Recorder()

    stats = run(RunConfig(max_articles=2), adapter=adapter, http=http, sink=recorder.sink())

    assert stats.articles_seen == 3  # what discovery found
    assert stats.articles_attempted == 2  # what this run was allowed to do
    assert http.text_urls == [entries[0].source_url, entries[1].source_url]
    # The two it did are marked; the third stays unmarked and comes back
    # tomorrow. That is what makes a chunked backfill advance.
    assert len(recorder.completions) == 2


def test_max_pages_is_threaded_into_discovery() -> None:
    adapter = FakeAdapter([])
    run(RunConfig(max_pages=3), adapter=adapter, http=FakeHttp(), sink=Recorder().sink())

    assert adapter.discovery_calls[0]["max_pages"] == 3


# --------------------------------------------------------------------------
# Failure isolation
# --------------------------------------------------------------------------


def test_one_failing_article_is_counted_and_the_run_continues() -> None:
    broken, healthy = listing_entry(1), listing_entry(2)
    adapter = FakeAdapter(
        [broken, healthy],
        {
            # What a restyled article page looks like from here.
            broken.source_url: ValueError("no breadcrumb found — page layout changed"),
            healthy.source_url: article_data(healthy),
        },
    )
    recorder = Recorder()

    stats = run(RunConfig(), adapter=adapter, http=FakeHttp(), sink=recorder.sink())

    assert stats.articles_new == 1
    assert stats.articles_failed == 1
    assert recorder.names.count("upsert_article") == 1
    assert exit_code(stats) == 0  # one success is a healthy run, not a red one


def test_one_failing_image_does_not_abort_its_article() -> None:
    entry = listing_entry(1)
    adapter = FakeAdapter(
        [entry], {entry.source_url: article_data(entry, [IMAGE_A, IMAGE_B, IMAGE_C])}
    )
    http = FakeHttp(
        bodies={
            IMAGE_A: HttpError("GET a.jpg: giving up after 3 attempts"),  # transport
            IMAGE_B: NOT_AN_IMAGE,  # decodes to nothing -> ImageError
            IMAGE_C: RED,
        }
    )
    recorder = Recorder()

    stats = run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    assert stats.articles_new == 1
    assert stats.images_failed == 2
    assert stats.images_uploaded == 1
    assert [row.source_image_url for row in recorder.image_rows] == [IMAGE_C]


def test_a_storage_failure_costs_the_images_not_the_article() -> None:
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A])})
    http = FakeHttp(bodies={IMAGE_A: RED})
    recorder = Recorder(fail_uploads=True)

    stats = run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    assert stats.articles_new == 1
    assert stats.images_failed == 1
    assert stats.bytes_uploaded == 0
    assert recorder.image_rows == []
    assert recorder.completions == []  # and it is not done, so it comes back


def test_an_upload_failure_leaves_the_same_bytes_retryable_later_in_the_article() -> None:
    """A hash is banked once its object is in the bucket, never before.

    Banking it at download time instead would make the second copy of a picture
    whose first upload was rejected a *duplicate* rather than a retry: the
    article reports one failure and one skip, so it is never marked complete,
    and every future run re-fetches it and reaches the same dead end. The two
    outcomes differ only in the tallies and the rows, which is why nothing else
    here would notice.
    """
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A, IMAGE_B])})
    # The same picture twice in one body, so both share a content hash.
    http = FakeHttp(bodies={IMAGE_A: RED, IMAGE_B: RED})
    recorder = Recorder(fail_first_uploads=1)  # only IMAGE_A's full-size object

    stats = run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    assert stats.images_failed == 1
    assert stats.images_skipped == 0  # the repeat was retried, not written off
    assert stats.images_uploaded == 1
    assert [row.source_image_url for row in recorder.image_rows] == [IMAGE_B]
    assert stats.bytes_uploaded == sum(len(body) for _path, body in recorder.uploads)


def test_a_failed_article_reports_no_images_it_never_downloaded() -> None:
    """The default ImageBatch is what every failed article returns.

    _process charges a failure to ArticleOutcome(stored=False), whose images
    field is the default batch, so a non-zero default is not a cosmetic detail:
    fifty articles that died in the parser would report fifty skipped or failed
    images nobody ever asked the CDN for, and the second exit_code branch reads
    exactly those counters.
    """
    entries = [listing_entry(1), listing_entry(2)]
    adapter = FakeAdapter(
        entries, {e.source_url: ValueError("no breadcrumb found") for e in entries}
    )
    http = FakeHttp()

    stats = run(RunConfig(), adapter=adapter, http=http, sink=Recorder().sink())

    assert stats.articles_failed == 2
    assert (stats.images_uploaded, stats.images_skipped, stats.images_failed) == (0, 0, 0)
    assert stats.bytes_uploaded == 0
    assert http.byte_urls == []  # nothing was fetched, so nothing may be counted
    assert exit_code(stats) == 1  # the article branch, on its own terms


def test_image_failures_earlier_in_a_run_still_turn_it_red() -> None:
    """images_failed is a run total, and the health check keys off it.

    A tally that kept only the last article's number would report whatever the
    final article failed — normally nothing — so a 200-article run that lost
    every image to a 403 would exit 0. The last article here has no images at
    all, which is the ordinary way that final number comes out zero.
    """
    first, second, third = listing_entry(1), listing_entry(2), listing_entry(3)
    adapter = FakeAdapter(
        [first, second, third],
        {
            first.source_url: article_data(first, [IMAGE_A]),
            second.source_url: article_data(second, [IMAGE_B]),
            third.source_url: article_data(third),  # text-only: nothing to fail
        },
    )
    http = FakeHttp(
        bodies={IMAGE_A: HttpError("GET a.jpg: 403"), IMAGE_B: HttpError("GET b.jpg: 403")}
    )

    stats = run(RunConfig(), adapter=adapter, http=http, sink=Recorder().sink())

    assert stats.articles_new == 3  # every article parsed and stored
    assert stats.images_failed == 2
    assert stats.images_uploaded == 0
    assert exit_code(stats) == 1


# --------------------------------------------------------------------------
# What each write is addressed to
# --------------------------------------------------------------------------


def test_objects_reach_storage_before_the_rows_that_point_at_them() -> None:
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A])})
    http = FakeHttp(bodies={IMAGE_A: RED})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    names = recorder.names
    # The article row necessarily comes first: its uuid namespaces every path.
    assert names.index("upsert_article") < names.index("upload")
    # Every object is in the bucket before any row references it.
    last_upload = len(names) - 1 - names[::-1].index("upload")
    assert last_upload < names.index("upsert_images")
    # Both derivatives of one source image are uploaded: full and thumb.
    assert names.count("upload") == 2


def test_each_derivative_gets_its_own_key_its_own_bytes_and_its_own_url() -> None:
    """The 600px thumbnail and the 1600px full image are two distinct objects.

    Everything here is one mistake away from silent: a thumb written to the
    full image's key overwrites it, and every lightbox in the gallery then
    serves a 600px picture at full size. The paths, the bytes behind them and
    the URLs that end up on the row are therefore all pinned against a second
    run of the same real optimizer.
    """
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A])})
    http = FakeHttp(bodies={IMAGE_A: RED})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    optimized = images.optimize(RED)  # same input, same real optimizer
    article_id = recorder.article_ids[0]
    full_path = storage.object_path(article_id, optimized.content_hash)
    thumb_path = storage.object_path(article_id, optimized.content_hash, thumb=True)
    assert full_path != thumb_path

    # Two keys, and the right bytes under each: the full image is the larger of
    # the pair, so a swapped body is not merely a different string.
    assert recorder.uploads == [(full_path, optimized.full), (thumb_path, optimized.thumb)]
    assert len(optimized.full) > len(optimized.thumb)

    (row,) = recorder.image_rows
    assert row.storage_path == full_path
    assert row.public_url == public_url_for(full_path)
    assert row.thumb_url == public_url_for(thumb_path)
    assert row.public_url != row.thumb_url
    # images.width/height describe `full`, which is what the lightbox opens.
    assert (row.width, row.height) == (optimized.width, optimized.height)
    assert row.content_hash == optimized.content_hash
    assert row.source_image_url == IMAGE_A


def test_every_write_is_addressed_to_the_id_its_own_article_row_returned() -> None:
    """Two articles, two ids, and nothing crossing between them.

    The bucket is public and relies on unguessable paths; the article uuid is
    what makes them unguessable. A namespace that stopped varying per article
    would collapse the whole archive under one prefix — and each article's
    credits and image rows would be filed against the wrong row besides.
    """
    first, second = listing_entry(1), listing_entry(2)
    adapter = FakeAdapter(
        [first, second],
        {
            first.source_url: article_data(first, [IMAGE_A]),
            second.source_url: article_data(second, [IMAGE_C]),
        },
    )
    http = FakeHttp(bodies={IMAGE_A: RED, IMAGE_C: BLUE})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    first_id, second_id = recorder.article_ids
    assert first_id != second_id

    # Every object sits under the prefix of the article it belongs to, and
    # nothing sits anywhere else.
    for article_id in (first_id, second_id):
        under_prefix = [p for p in recorder.upload_paths if p.startswith(f"{article_id}/")]
        assert len(under_prefix) == 2  # full + thumb
    assert len(recorder.upload_paths) == 4

    assert [article_id for article_id, _ in recorder.credit_calls] == [first_id, second_id]
    assert [article_id for article_id, _ in recorder.image_batches] == [first_id, second_id]
    assert [article_id for article_id, _ in recorder.completions] == [first_id, second_id]
    assert [row.storage_path.split("/")[0] for row in recorder.image_rows] == [
        first_id,
        second_id,
    ]


def test_the_parsed_credits_are_written_for_the_article_they_belong_to() -> None:
    """Credits are a v1 feature and the basis of the v1.1 credit-person filter.

    Dropping the call, or making it with an empty tuple, costs the gallery its
    photographer and stylist bylines and leaves no other trace.
    """
    entry = listing_entry(1)
    data = article_data(entry)
    adapter = FakeAdapter([entry], {entry.source_url: data})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=FakeHttp(), sink=recorder.sink())

    assert data.credits  # the fixture would otherwise prove nothing
    assert recorder.credit_calls == [(recorder.article_ids[0], data.credits)]


def test_credits_are_replaced_before_the_images_are_fetched() -> None:
    # Cheap and certain first: the credits are already parsed, while the image
    # loop is the long, failure-prone part of the article.
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A])})
    http = FakeHttp(bodies={IMAGE_A: RED})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    names = recorder.names
    assert names.index("upsert_article") < names.index("replace_credits") < names.index("upload")


def test_duplicate_images_are_collapsed_before_the_batch_upsert() -> None:
    entry = listing_entry(1)
    # The same picture twice in one body: two rows sharing
    # (article_id, content_hash) in one INSERT fail with cardinality_violation.
    adapter = FakeAdapter(
        [entry], {entry.source_url: article_data(entry, [IMAGE_A, IMAGE_B, IMAGE_C])}
    )
    http = FakeHttp(bodies={IMAGE_A: RED, IMAGE_B: RED, IMAGE_C: BLUE})
    recorder = Recorder()

    stats = run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    assert stats.images_uploaded == 2
    assert stats.images_skipped == 1
    hashes = [row.content_hash for row in recorder.image_rows]
    assert len(hashes) == len(set(hashes)) == 2
    # First occurrence wins, so the repeat keeps the reading order it was found in.
    assert [row.position for row in recorder.image_rows] == [1, 3]
    assert recorder.names.count("upload") == 4  # two survivors, full + thumb each
    # A duplicate is not a failure, so the article still finishes.
    assert len(recorder.completions) == 1


def test_positions_keep_their_gaps_when_an_image_fails() -> None:
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A, IMAGE_B])})
    http = FakeHttp(bodies={IMAGE_A: NOT_AN_IMAGE, IMAGE_B: RED})
    recorder = Recorder()

    run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    # Renumbering would move this picture on the next run, once the first one
    # downloads; source order is stable and nothing requires contiguity.
    assert [row.position for row in recorder.image_rows] == [2]


# --------------------------------------------------------------------------
# The byte meter and the free-tier gauge
# --------------------------------------------------------------------------


def test_the_byte_meter_counts_both_derivatives_of_every_image() -> None:
    """bytes_uploaded is measured against what the sink was actually handed.

    Counting the full image alone would under-report by roughly the thumbnail,
    and double-counting either would over-report, in both cases by a factor
    that only shows up once the bucket is much fuller than the gauge says.
    """
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A, IMAGE_C])})
    http = FakeHttp(bodies={IMAGE_A: RED, IMAGE_C: BLUE})
    recorder = Recorder()

    stats = run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    # Four objects for two source images, and the meter is their sum exactly.
    assert len(recorder.uploads) == 4
    assert stats.bytes_uploaded == sum(len(body) for _path, body in recorder.uploads)
    # And the same total the real optimizer produces for the same input, so a
    # meter that happened to match a mis-sized upload is still caught.
    red, blue = images.optimize(RED), images.optimize(BLUE)
    assert stats.bytes_uploaded == (
        len(red.full) + len(red.thumb) + len(blue.full) + len(blue.thumb)
    )


@pytest.mark.parametrize(
    ("uploaded", "expected_mib", "expected_share"),
    [
        (FREE_TIER_BYTES // 2, "512.0 MiB", "50.00%"),
        (FREE_TIER_BYTES // 100, "10.2 MiB", "1.00%"),
        (BYTES_PER_MIB, "1.0 MiB", "0.10%"),
        (0, "0.0 MiB", "0.00%"),
    ],
)
def test_the_free_tier_gauge_is_a_share_of_one_gib(
    uploaded: int, expected_mib: str, expected_share: str
) -> None:
    """Realistic magnitudes, because that is where every wrong term shows.

    A run's own byte total is a few MiB, where a percentage of the wrong
    denominator still prints as 0.00% and a MiB conversion that never happened
    is only two digits out. Against half the tier they are unmistakable — and
    "0.04% of the 1 GiB free tier" from a run that used 40% of it is precisely
    the report that would let the project sail past the ceiling
    PHASE0_AMENDMENTS §E names as its binding constraint.
    """
    line = RunStats(bytes_uploaded=uploaded).storage_budget_line()

    assert expected_mib in line
    assert expected_share in line
    assert "1 GiB" in line


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing_but_still_measures_the_run() -> None:
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A])})
    http = FakeHttp(bodies={IMAGE_A: RED})
    recorder = Recorder()

    stats = run(
        RunConfig(dry_run=True),
        adapter=adapter,
        http=http,
        sink=without_writes(recorder.sink()),
    )

    # The read still happens — a dry run should process the articles a real run
    # would — and every write is gone, the completion marker included: stamping
    # one for images that were never uploaded would strand the article.
    assert recorder.names == ["completed_source_urls"]
    assert recorder.completions == []
    # The work upstream of the writes still ran, so the byte cost is real.
    assert http.byte_urls == [IMAGE_A]
    assert stats.articles_new == 1
    assert stats.images_uploaded == 1
    assert stats.bytes_uploaded > 0


def test_the_dry_run_sink_never_reaches_db_or_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a dry run reached a write")

    monkeypatch.setattr(db, "get_client", lambda: object())  # never used, never real
    monkeypatch.setattr(db, "upsert_article", boom)
    monkeypatch.setattr(db, "replace_credits", boom)
    monkeypatch.setattr(db, "upsert_images", boom)
    monkeypatch.setattr(db, "mark_article_complete", boom)
    monkeypatch.setattr(storage, "upload", boom)

    sink = build_sink(dry_run=True)
    entry = listing_entry(1)

    assert sink.upsert_article(article_data(entry)) == DRY_RUN_ARTICLE_ID
    assert sink.upload("some/path.webp", b"bytes").startswith(DRY_RUN_URL_PREFIX)
    assert sink.replace_credits(DRY_RUN_ARTICLE_ID, ()) is None
    assert sink.upsert_images(DRY_RUN_ARTICLE_ID, ()) is None
    assert sink.mark_article_complete(DRY_RUN_ARTICLE_ID, "a" * 64) is None


def test_the_dry_run_stand_ins_are_shaped_like_the_values_they_replace() -> None:
    """Both constants are load-bearing shapes, not labels.

    The article id is what namespaces object paths, so an empty one turns
    storage.object_path into "/<hash>.webp" — a leading-slash key, and a
    different object from the one a real run would write — while a dry run's
    tallies and exit code stay identical. The URL prefix has to be recognisable
    as not-a-real-URL for the same reason: it is the one value a dry run puts
    where an https:// CDN link belongs.
    """
    sink = without_writes(Recorder().sink())

    article_id = sink.upsert_article(article_data(listing_entry(1)))
    assert article_id == "dry-run"

    path = storage.object_path(article_id, "a" * 64)
    assert path == f"dry-run/{'a' * 16}.webp"
    assert not path.startswith("/")

    assert DRY_RUN_URL_PREFIX == "dry-run://"
    assert sink.upload(path, b"bytes") == f"dry-run://{path}"


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_the_sink_calls_db_and_storage_the_way_they_are_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sink is where this module's calls into db and storage are spelled out.

    A signature that drifts on either side of that seam would otherwise stay
    invisible until a live run failed on every article at once.
    """
    client = object()  # intercepted before anything touches it
    seen: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def record(name: str, result: Any = None) -> Any:
        def fake(*args: Any, **kwargs: Any) -> Any:
            seen.append((name, args, kwargs))
            return result

        return fake

    monkeypatch.setattr(db, "completed_source_urls", record("completed_source_urls", {"u"}))
    monkeypatch.setattr(db, "upsert_article", record("upsert_article", "id-1"))
    monkeypatch.setattr(db, "replace_credits", record("replace_credits"))
    monkeypatch.setattr(db, "upsert_images", record("upsert_images"))
    monkeypatch.setattr(db, "mark_article_complete", record("mark_article_complete"))
    monkeypatch.setattr(storage, "upload", record("upload", "https://cdn.example.test/x.webp"))

    sink = supabase_sink(client)
    data = article_data(listing_entry(1))

    assert sink.completed_source_urls() == {"u"}
    assert sink.upsert_article(data) == "id-1"
    sink.replace_credits("id-1", data.credits)
    assert sink.upload("some/path.webp", b"bytes") == "https://cdn.example.test/x.webp"
    sink.upsert_images("id-1", ())
    sink.mark_article_complete("id-1", "a" * 64)

    assert [name for name, _, _ in seen] == [
        "completed_source_urls",
        "upsert_article",
        "replace_credits",
        "upload",
        "upsert_images",
        "mark_article_complete",
    ]
    # Every call is made against the one client the run was given...
    assert all(args[0] is client for _, args, _ in seen)
    # ...and each carries the arguments db declares, positionally.
    assert seen[1][1][1:] == (data,)
    assert seen[2][1][1:] == ("id-1", data.credits)
    assert seen[5][1][1:] == ("id-1", "a" * 64)


def test_a_real_run_gets_a_sink_whose_writes_reach_db_and_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run is the only thing allowed to disconnect the writes.

    Nothing else in this file watches the live path: every write assertion
    drives a Sink the test itself built, and the dry-run tests assert only that
    the dry path writes nothing. A build_sink that handed back the dry sink
    unconditionally would keep all of them green — upsert_article would answer
    a constant id, upload would fabricate a URL, articles_new would climb and
    CI would stay green while the gallery never gained a single image.
    """
    client = object()  # intercepted before anything touches it
    seen: list[tuple[str, tuple[Any, ...]]] = []

    def record(name: str, result: Any = None) -> Any:
        def fake(*args: Any, **kwargs: Any) -> Any:
            seen.append((name, args))
            return result

        return fake

    monkeypatch.setattr(db, "get_client", lambda: client)
    monkeypatch.setattr(db, "completed_source_urls", record("completed_source_urls", {"u"}))
    monkeypatch.setattr(db, "upsert_article", record("upsert_article", "real-article-id"))
    monkeypatch.setattr(db, "replace_credits", record("replace_credits"))
    monkeypatch.setattr(db, "upsert_images", record("upsert_images"))
    monkeypatch.setattr(db, "mark_article_complete", record("mark_article_complete"))
    monkeypatch.setattr(storage, "upload", record("upload", "https://cdn.example.test/x.webp"))

    sink = build_sink(dry_run=False)
    data = article_data(listing_entry(1))

    assert sink.completed_source_urls() == {"u"}
    # Both answers come back from the layer below rather than from a constant:
    # these are the two returns a dry sink invents.
    assert sink.upsert_article(data) == "real-article-id"
    sink.replace_credits("real-article-id", data.credits)
    assert sink.upload("some/path.webp", b"bytes") == "https://cdn.example.test/x.webp"
    sink.upsert_images("real-article-id", ())
    sink.mark_article_complete("real-article-id", "a" * 64)

    # Every write arrived, in order, at the module that owns it.
    assert [name for name, _ in seen] == [
        "completed_source_urls",
        "upsert_article",
        "replace_credits",
        "upload",
        "upsert_images",
        "mark_article_complete",
    ]
    assert all(args[0] is client for _, args in seen)


def test_a_dry_run_still_reads_through_the_client_a_real_run_would_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client is built either way, and the read goes through it.

    That is what makes a dry run's article list the list a real run would
    process: skip the client and the completed permalinks come back empty, so
    the dry run cheerfully re-processes the whole archive and reports a byte
    cost and an article count no real run would ever produce.
    """
    client = object()
    clients: list[Any] = []
    completed = {listing_entry(1).source_url}

    def completed_source_urls(passed_client: Any) -> set[str]:
        clients.append(passed_client)
        return set(completed)

    monkeypatch.setattr(db, "get_client", lambda: client)
    monkeypatch.setattr(db, "completed_source_urls", completed_source_urls)

    sink = build_sink(dry_run=True)

    assert sink.completed_source_urls() == completed
    assert clients == [client]


# --------------------------------------------------------------------------
# Run health
# --------------------------------------------------------------------------


def test_discovering_articles_and_parsing_none_exits_non_zero() -> None:
    entries = [listing_entry(1), listing_entry(2)]
    adapter = FakeAdapter(
        entries,
        {e.source_url: ValueError("no breadcrumb found — page layout changed") for e in entries},
    )
    recorder = Recorder()

    stats = run(RunConfig(), adapter=adapter, http=FakeHttp(), sink=recorder.sink())

    assert stats.articles_new == 0
    assert stats.articles_failed == 2
    assert exit_code(stats) == 1
    assert "upsert_article" not in recorder.names


def test_a_run_where_every_image_failed_exits_non_zero() -> None:
    """The articles parsed, so the first health branch stays quiet.

    This is what GQ's CDN starting to demand a Referer looks like, and what the
    Storage bucket crossing the 1 GiB free tier looks like: a run that stores a
    hundred imageless articles and, without this branch, exits 0.
    """
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A, IMAGE_B])})
    http = FakeHttp(bodies={IMAGE_A: HttpError("GET a.jpg: 403"), IMAGE_B: HttpError("403")})
    recorder = Recorder()

    stats = run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    assert stats.articles_new == 1  # so the article-side branch cannot be what fired
    assert stats.images_failed == 2
    assert stats.images_uploaded == 0
    assert exit_code(stats) == 1


def test_an_article_that_simply_has_no_images_keeps_the_run_green() -> None:
    # Nothing was attempted, so nothing failed. Firing here would turn every
    # text-only article into a red nightly run.
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry)})
    recorder = Recorder()

    stats = run(RunConfig(), adapter=adapter, http=FakeHttp(), sink=recorder.sink())

    assert stats.images_failed == 0
    assert stats.images_uploaded == 0
    assert exit_code(stats) == 0


def test_one_surviving_image_keeps_the_run_green() -> None:
    # A single dead image URL is ordinary; only a total wipe-out is an outage.
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A, IMAGE_B])})
    http = FakeHttp(bodies={IMAGE_A: HttpError("GET a.jpg: 403"), IMAGE_B: RED})
    recorder = Recorder()

    stats = run(RunConfig(), adapter=adapter, http=http, sink=recorder.sink())

    assert (stats.images_uploaded, stats.images_failed) == (1, 1)
    assert exit_code(stats) == 0


def test_a_run_with_nothing_new_exits_zero() -> None:
    recorder = Recorder(completed={listing_entry(1).source_url})
    adapter = FakeAdapter([listing_entry(1)])

    stats = run(RunConfig(), adapter=adapter, http=FakeHttp(), sink=recorder.sink())

    assert stats.articles_seen == 0
    assert exit_code(stats) == 0  # the ordinary incremental outcome
    assert recorder.names == ["completed_source_urls"]


def test_summary_line_carries_every_counter() -> None:
    summary = RunStats(
        articles_seen=3,
        articles_new=2,
        articles_failed=1,
        images_uploaded=7,
        images_skipped=1,
        images_failed=2,
        bytes_uploaded=1234,
        elapsed_seconds=4.25,
    ).summary()

    for field_text in (
        "articles_seen=3",
        "articles_new=2",
        "articles_failed=1",
        "images_uploaded=7",
        "images_skipped=1",
        "images_failed=2",
        "bytes_uploaded=1234",
        "elapsed_seconds=4.2",
    ):
        assert field_text in summary


def test_every_tally_accumulates_across_the_articles_of_one_run() -> None:
    """RunStats describes the run, not the last article of it.

    Each counter here is a fresh total, so a term that stopped adding would
    report the final article's number and nothing else: bytes_uploaded feeds
    the free-tier gauge, images_failed feeds the second exit_code branch, and
    both would then describe one article out of two hundred. The three
    contributions are deliberately distinct and none of them is the total.
    """
    outcomes = (
        ArticleOutcome(
            stored=True,
            images=ImageBatch(rows=(image_row(1),), skipped=1, failed=2, bytes_uploaded=100),
        ),
        ArticleOutcome(
            stored=True,
            images=ImageBatch(
                rows=(image_row(2), image_row(3)), skipped=3, failed=4, bytes_uploaded=1_000
            ),
        ),
        # A failed article still carries a batch, and it still has to be added.
        ArticleOutcome(stored=False, images=ImageBatch(skipped=5, failed=6, bytes_uploaded=10_000)),
    )

    stats = RunStats()
    for outcome in outcomes:
        stats = stats.with_outcome(outcome)

    assert stats.articles_new == 2
    assert stats.articles_failed == 1
    assert stats.articles_attempted == 3
    assert stats.images_uploaded == 3
    assert stats.images_skipped == 9
    assert stats.images_failed == 12
    assert stats.bytes_uploaded == 11_100


def test_the_closing_report_is_emitted_and_not_merely_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A summary nobody logs is a summary nobody reads.

    run() returns its stats to main(), which does nothing with them but derive
    an exit code, so the log is the only place a human ever sees what a nightly
    run did — and the free-tier gauge is the only warning the project gets that
    it is approaching its one hard ceiling. Both lines are pinned here as
    emitted records, at a level an INFO-configured CI run prints.
    """
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: article_data(entry, [IMAGE_A])})
    http = FakeHttp(bodies={IMAGE_A: RED})

    # DEBUG, so a report emitted below INFO is captured and then judged, rather
    # than dropped and mistaken for one that was never emitted at all.
    with caplog.at_level(logging.DEBUG, logger="gallery_scraper.pipeline"):
        stats = run(RunConfig(), adapter=adapter, http=http, sink=Recorder().sink())

    emitted = {record.getMessage(): record for record in pipeline_records(caplog)}
    assert stats.summary() in emitted
    assert stats.storage_budget_line() in emitted
    assert stats.bytes_uploaded > 0  # the gauge would otherwise prove nothing
    for line in (stats.summary(), stats.storage_budget_line()):
        assert emitted[line].levelno >= logging.INFO


def test_a_failed_article_is_logged_with_the_traceback_that_says_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The traceback is the diagnosis; the URL alone is only the symptom.

    _process swallows every exception on purpose so one restyled article cannot
    end a run, which makes this record the only surviving evidence of what
    moved. Without exc_info a red nightly run says "50 articles failed" and
    nothing whatsoever about which selector stopped matching.
    """
    entry = listing_entry(1)
    adapter = FakeAdapter([entry], {entry.source_url: ValueError("no breadcrumb found")})

    with caplog.at_level(logging.DEBUG, logger="gallery_scraper.pipeline"):
        stats = run(RunConfig(), adapter=adapter, http=FakeHttp(), sink=Recorder().sink())

    assert stats.articles_failed == 1
    (failure,) = [record for record in pipeline_records(caplog) if record.exc_info]
    assert entry.source_url in failure.getMessage()  # which article
    assert failure.levelno >= logging.WARNING  # and it is not routine output
    assert failure.exc_info[0] is ValueError
    # The formatted record carries the frames, not just the type: that is what
    # names the line of the parser the markup moved under.
    assert "Traceback (most recent call last)" in caplog.text
    assert "no breadcrumb found" in caplog.text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_default_arguments_describe_a_scheduled_incremental_run() -> None:
    """Pinned to the shared defaults, not merely bounded.

    "at least one page" is satisfied by a default of 1, which would turn every
    scheduled run into a walk of the newest listing page only: a backfill could
    never advance past the newest 50 articles, and an article left incomplete
    behind them could never be reached again. The pace has the same shape of
    problem at the other end — any positive number passes a `> 0` bound,
    including one far below what DEFAULT_MIN_INTERVAL commits us to.
    """
    config = parse_args([])

    assert config.max_articles is None  # uncapped: the workflow supplies the cap
    assert config.dry_run is False
    assert config.max_pages == DEFAULT_MAX_PAGES
    assert DEFAULT_MAX_PAGES > 1  # enough pages for a backfill to make progress
    assert config.min_interval == DEFAULT_MIN_INTERVAL
    assert DEFAULT_MIN_INTERVAL > 0


def test_arguments_are_parsed() -> None:
    config = parse_args(
        ["--max-articles", "25", "--max-pages", "4", "--dry-run", "--min-interval", "2.5"]
    )

    assert config == RunConfig(max_articles=25, max_pages=4, dry_run=True, min_interval=2.5)


@pytest.mark.parametrize("argv", [["--max-articles", "0"], ["--max-pages", "-1"]])
def test_non_positive_caps_are_rejected(argv: list[str]) -> None:
    # A cap of zero would process nothing and then report "every article
    # failed", turning a typo into a red run for the wrong reason.
    with pytest.raises(SystemExit):
        parse_args(argv)


@pytest.mark.parametrize("value", ["-5", "0", "nan"])
def test_a_min_interval_that_is_not_a_pace_is_rejected(value: str) -> None:
    """Politeness is the one thing this scraper cannot be talked out of.

    A bare `type=float` accepts every one of these and hands PoliteClient a
    floor that is no floor: negative and zero both mean "as fast as the socket
    allows", and NaN compares false against everything the pacing code asks of
    it. GQ's robots.txt disallows named crawlers outright (PHASE0_AMENDMENTS
    §F) — behaving is the whole of what keeps a personal archive welcome, so a
    fat-fingered workflow input has to stop the run rather than silently
    remove the pacing.
    """
    with pytest.raises(SystemExit):
        parse_args(["--min-interval", value])


def test_a_positive_min_interval_is_still_accepted() -> None:
    # The validator rejects, it does not round: sub-second paces are legitimate
    # for a local dry run against a handful of articles.
    assert parse_args(["--min-interval", "0.25"]).min_interval == 0.25


def test_there_is_no_full_flag() -> None:
    # Deliberately removed, not overlooked. --full cleared the skip set, so
    # combined with --max-articles it re-did the newest N on every run and a
    # backfill could never advance. Re-scraping a range is now one statement
    # against the database — `update articles set content_hash = null where …`
    # — after which ordinary capped runs work through it and resume.
    with pytest.raises(SystemExit):
        parse_args(["--full"])


# --------------------------------------------------------------------------
# main() — the entry point scrape.yml actually invokes
# --------------------------------------------------------------------------


@dataclass
class MainHarness:
    """The three collaborators main() constructs, replaced and recorded.

    main() is the only code path CI runs, and every one of its wiring decisions
    is invisible from anywhere else: which flags reach the run, whether the
    dry-run flag reaches the sink, whether the adapter shares the run's one
    rate-limited client, and whether the process exit status is the health
    check's answer or a hard-coded zero.
    """

    http: FakeHttp
    adapter: FakeAdapter
    recorder: Recorder
    http_kwargs: list[dict[str, Any]] = field(default_factory=list)
    adapter_clients: list[Any] = field(default_factory=list)
    sink_kwargs: list[dict[str, Any]] = field(default_factory=list)


def install_main_wiring(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: Sequence[ListingEntry],
    articles: Mapping[str, ArticleData | Exception],
    bodies: Mapping[str, bytes | Exception] | None = None,
) -> MainHarness:
    """Replace PoliteClient, GqKoreaAdapter and build_sink where main resolves them."""
    harness = MainHarness(
        http=FakeHttp(bodies=bodies or {}),
        adapter=FakeAdapter(entries, articles),
        recorder=Recorder(),
    )

    def fake_polite_client(**kwargs: Any) -> FakeHttp:
        harness.http_kwargs.append(kwargs)
        return harness.http

    # `client=None` on purpose: constructing the adapter without the run's
    # client is a real mistake to make, and the fake has to let it happen so
    # the assertion below can be the thing that catches it.
    def fake_adapter(client: Any = None) -> FakeAdapter:
        harness.adapter_clients.append(client)
        return harness.adapter

    def fake_build_sink(**kwargs: Any) -> Sink:
        harness.sink_kwargs.append(kwargs)
        return harness.recorder.sink()

    monkeypatch.setattr(pipeline, "PoliteClient", fake_polite_client)
    monkeypatch.setattr(pipeline, "GqKoreaAdapter", fake_adapter)
    monkeypatch.setattr(pipeline, "build_sink", fake_build_sink)
    return harness


def test_main_hands_every_parsed_flag_to_the_collaborator_that_needs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [listing_entry(1), listing_entry(2)]
    harness = install_main_wiring(
        monkeypatch,
        entries=entries,
        articles={e.source_url: article_data(e) for e in entries},
    )

    status = main(
        ["--max-articles", "1", "--max-pages", "2", "--dry-run", "--min-interval", "0.25"]
    )

    assert status == 0
    # The dry-run flag has to reach build_sink, or --dry-run writes to the
    # live project.
    assert harness.sink_kwargs == [{"dry_run": True}]
    # The politeness budget has to reach the client, or the site is hit at the
    # default pace whatever the workflow asked for.
    assert harness.http_kwargs == [{"min_interval": 0.25}]
    # One client for the whole run: an adapter with its own would give the site
    # two independent rate limits.
    assert harness.adapter_clients == [harness.http]
    # And the caps have to reach the run itself.
    assert harness.adapter.discovery_calls[0]["max_pages"] == 2
    assert len(harness.recorder.article_ids) == 1
    assert harness.http.closed  # the `with` block, so sockets are released


def test_main_returns_the_health_checks_answer_not_a_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every article failed to parse: the run itself completed, so only
    # exit_code can turn this red, and only main can carry that out to CI.
    entries = [listing_entry(1), listing_entry(2)]
    harness = install_main_wiring(
        monkeypatch,
        entries=entries,
        articles={e.source_url: ValueError("page layout changed") for e in entries},
    )

    assert main([]) == 1
    assert harness.recorder.article_ids == []


def test_main_is_green_when_the_run_is(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = listing_entry(1)
    install_main_wiring(
        monkeypatch,
        entries=[entry],
        articles={entry.source_url: article_data(entry, [IMAGE_A])},
        bodies={IMAGE_A: RED},
    )

    assert main([]) == 0


def test_main_configures_logging_so_the_run_report_reaches_ci(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """main() is the only place the process's log level is decided.

    Raising it by one step deletes the summary, the per-article progress lines
    and the free-tier gauge from every CI run at once, and leaves every
    counter, every write and the exit status untouched — a change no other
    assertion in this file can see. The pin is the relation, not the constant:
    whatever level main configures has to be low enough to show what run()
    emits.
    """
    configured: list[int] = []

    def record_basic_config(**kwargs: Any) -> None:
        configured.append(kwargs["level"])

    # basicConfig itself is a no-op under pytest — the root logger already has
    # handlers — so what it was *asked* for is the only observable thing here.
    monkeypatch.setattr(logging, "basicConfig", record_basic_config)
    entry = listing_entry(1)
    install_main_wiring(
        monkeypatch, entries=[entry], articles={entry.source_url: article_data(entry)}
    )

    with caplog.at_level(logging.DEBUG, logger="gallery_scraper.pipeline"):
        assert main([]) == 0

    (level,) = configured
    (summary,) = [
        record
        for record in pipeline_records(caplog)
        if record.getMessage().startswith("run summary")
    ]
    assert level <= summary.levelno


def test_main_turns_an_aborted_run_red_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Absent credentials, an unreachable Supabase, a renamed taxonomy: each
    # means this run scraped nothing, and CI has to see it.
    def unreachable(**_kwargs: Any) -> Sink:
        raise db.ConfigError("SUPABASE_URL is missing or empty in the environment")

    install_main_wiring(monkeypatch, entries=[], articles={})
    monkeypatch.setattr(pipeline, "build_sink", unreachable)

    assert main([]) == 1


def test_main_rejects_a_bad_flag_before_it_builds_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # argparse exits rather than returning, and it must do so before a Supabase
    # client or an HTTP session exists.
    harness = install_main_wiring(monkeypatch, entries=[], articles={})

    with pytest.raises(SystemExit):
        main(["--max-articles", "0"])

    assert harness.sink_kwargs == []
    assert harness.http_kwargs == []
