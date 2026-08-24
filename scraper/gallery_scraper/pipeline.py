"""One scrape run: discover -> fetch -> parse -> optimize -> Storage -> Postgres.

The other modules each own one boundary and know nothing about the run; this is
where the run lives. Three decisions shape it:

**One PoliteClient for everything.** Rate limiting is per instance, so listing
pages, article pages and image downloads all go through the same client or the
site sees the sum of two independent paces.

**Every write goes through `Sink`.** Reads and writes to Supabase are bundled
behind one small seam, which keeps the flow free of `if dry_run` branches — a
dry run is simply a sink whose writes do nothing — and lets the tests watch the
whole conversation, in order, without a Supabase client anywhere.

**A failure is charged to the smallest thing that can absorb it.** One restyled
article must not end a run and one dead image URL must not lose an article, so
both are counted and skipped. Discovery failures are the exception: a renamed
taxonomy means the run saw nothing at all, and it is raised, not tallied.

**An article counts as done only when its images are in the bucket.** The run
skips what `db.completed_source_urls` reports and marks an article complete only
after a zero-failure image batch, so a capped run, a run the CI timeout killed
and an article whose CDN answered 403 all leave work the *next* run picks up.
There is deliberately no --full: forcing a re-scrape is one statement against
the database — `update articles set content_hash = null where ...` — after which
ordinary runs redo exactly those articles, in --max-articles-sized chunks, and
resume where they stopped. The flag could not do that; capped, it re-did the
newest N forever.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from gallery_scraper import db, images, storage
from gallery_scraper.core.adapter import (
    DEFAULT_MAX_PAGES,
    ArticleData,
    Credit,
    ImageRef,
    ListingEntry,
    SiteAdapter,
)
from gallery_scraper.core.http import DEFAULT_MIN_INTERVAL, HttpError, PoliteClient
from gallery_scraper.images import ImageError, OptimizedImage
from gallery_scraper.sites.gq_korea import GqKoreaAdapter
from gallery_scraper.storage import StorageError

if TYPE_CHECKING:  # supabase stays out of the import path at runtime
    from supabase import Client

# Named rather than __name__: `python -m gallery_scraper.pipeline` runs this
# module as __main__, and a CI log that says "__main__" is one nobody can grep
# by module once a second logger exists.
LOG = logging.getLogger("gallery_scraper.pipeline")
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Supabase Storage's free tier, PHASE0_AMENDMENTS §E: about 400 articles at
# MAX_EDGE/THUMB_EDGE. Logged as a share of this because the run can measure
# what it added but cannot see what the bucket already holds.
FREE_TIER_BYTES = 1024**3
BYTES_PER_MIB = 1024**2

# A dry run still needs an article id to derive object paths from, and a public
# URL string that passes ImageRow's NOT NULL checks. Neither is ever stored.
DRY_RUN_ARTICLE_ID = "dry-run"
DRY_RUN_URL_PREFIX = "dry-run://"


@dataclass(frozen=True)
class RunConfig:
    """One run's knobs. `dry_run` is honoured by build_sink, not by run()."""

    max_articles: int | None = None
    max_pages: int = DEFAULT_MAX_PAGES
    dry_run: bool = False
    min_interval: float = DEFAULT_MIN_INTERVAL


@dataclass(frozen=True)
class Sink:
    """Everything the run reads from and writes to Supabase, behind one seam.

    mark_article_complete belongs here rather than beside db for one reason: it
    is a write, and routing it through the seam is what stops a --dry-run from
    stamping articles complete for images it never uploaded.
    """

    completed_source_urls: Callable[[], set[str]]
    upsert_article: Callable[[ArticleData], str]
    replace_credits: Callable[[str, Sequence[Credit]], None]
    upload: Callable[[str, bytes], str]
    upsert_images: Callable[[str, Sequence[db.ImageRow]], None]
    mark_article_complete: Callable[[str, str], None]


@dataclass(frozen=True)
class ImageBatch:
    """What one article's images cost and what survived to be written."""

    rows: tuple[db.ImageRow, ...] = ()
    skipped: int = 0  # duplicate content hashes, collapsed before the batch
    failed: int = 0
    bytes_uploaded: int = 0


@dataclass(frozen=True)
class ArticleOutcome:
    stored: bool
    images: ImageBatch = ImageBatch()


@dataclass(frozen=True)
class RunStats:
    articles_seen: int = 0  # what discovery returned, before --max-articles
    articles_new: int = 0
    articles_failed: int = 0
    images_uploaded: int = 0
    images_skipped: int = 0
    images_failed: int = 0
    bytes_uploaded: int = 0
    elapsed_seconds: float = 0.0

    @property
    def articles_attempted(self) -> int:
        return self.articles_new + self.articles_failed

    def with_outcome(self, outcome: ArticleOutcome) -> RunStats:
        """A new tally including one article's result."""
        return replace(
            self,
            articles_new=self.articles_new + (1 if outcome.stored else 0),
            articles_failed=self.articles_failed + (0 if outcome.stored else 1),
            images_uploaded=self.images_uploaded + len(outcome.images.rows),
            images_skipped=self.images_skipped + outcome.images.skipped,
            images_failed=self.images_failed + outcome.images.failed,
            bytes_uploaded=self.bytes_uploaded + outcome.images.bytes_uploaded,
        )

    def summary(self) -> str:
        """One greppable line — this is what a CI log is read for."""
        return (
            f"run summary articles_seen={self.articles_seen} "
            f"articles_new={self.articles_new} articles_failed={self.articles_failed} "
            f"images_uploaded={self.images_uploaded} images_skipped={self.images_skipped} "
            f"images_failed={self.images_failed} bytes_uploaded={self.bytes_uploaded} "
            f"elapsed_seconds={self.elapsed_seconds:.1f}"
        )

    def storage_budget_line(self) -> str:
        """What this run added, as a share of the Storage free tier.

        Its own line rather than another field of summary(), and its own method
        rather than three arguments to a log call, because it is the only number
        any run produces about the ceiling this project actually hits first
        (PHASE0_AMENDMENTS §E) — and every term of it is silently wrong-able.
        This run's addition only: nothing here can see what the bucket holds.
        """
        return (
            f"storage: {self.bytes_uploaded / BYTES_PER_MIB:.1f} MiB added this run, "
            f"{self.bytes_uploaded / FREE_TIER_BYTES * 100:.2f}% of the "
            f"{FREE_TIER_BYTES // 1024**3} GiB free tier"
        )


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def run(config: RunConfig, *, adapter: SiteAdapter, http: PoliteClient, sink: Sink) -> RunStats:
    """Scrape one batch of articles and return what it cost."""
    started = time.monotonic()
    if config.dry_run:
        LOG.info("dry run: articles are fetched and re-encoded, nothing is written")

    # Completed, not merely stored: an article whose images failed is absent
    # from this set on purpose, so discovery hands it back and this run finishes
    # it. See db.completed_source_urls.
    seen = frozenset(sink.completed_source_urls())
    LOG.info("discovering: max_pages=%d already_complete=%d", config.max_pages, len(seen))

    entries = adapter.discover_article_urls(max_pages=config.max_pages, seen=seen)
    stats = RunStats(articles_seen=len(entries))
    LOG.info("discovered %d article(s) worth fetching", stats.articles_seen)

    for entry in _capped(entries, config.max_articles):
        stats = stats.with_outcome(_process(entry, adapter=adapter, http=http, sink=sink))
        LOG.info(
            "progress: %d/%d article(s), %.1f MiB uploaded so far",
            stats.articles_attempted,
            stats.articles_seen,
            stats.bytes_uploaded / BYTES_PER_MIB,
        )

    stats = replace(stats, elapsed_seconds=time.monotonic() - started)
    LOG.info("%s", stats.summary())
    # The budget is worth a line of its own: the free tier is the ceiling this
    # project actually hits first (PHASE0_AMENDMENTS §E).
    LOG.info("%s", stats.storage_budget_line())
    return stats


def exit_code(stats: RunStats) -> int:
    """0 for a healthy run, 1 for one that looks broken rather than quiet.

    Nothing new is the ordinary incremental outcome and exits 0. Two things are
    not, and both are invisible in a green run unless they are named here
    (PLAN.md §Robustness):

      - every attempted article failed, while the listing still parsed: what
        moved is the article template;
      - every image the run touched failed: the articles parsed fine, so what
        moved is the CDN or the storage bucket. Without this branch a run that
        stored a hundred imageless articles would exit 0.

    An article that legitimately has no images fails nothing, so it cannot fire
    either branch.
    """
    if stats.articles_attempted and not stats.articles_new:
        LOG.error(
            "all %d attempted article(s) failed — the article markup has most likely "
            "changed (sites/gq_korea.py and its fixture), or Supabase is refusing "
            "writes; the per-article warnings above carry the traceback that says which",
            stats.articles_attempted,
        )
        return 1
    if stats.images_failed and not stats.images_uploaded:
        LOG.error(
            "all %d image(s) this run touched failed and none was stored — the two "
            "causes worth checking first are GQ's CDN starting to refuse downloads "
            "that carry no Referer (403 on every image) and the Storage bucket "
            "crossing the %d GiB free tier (every upload rejected); the per-image "
            "warnings above say which side answered",
            stats.images_failed,
            FREE_TIER_BYTES // 1024**3,
        )
        return 1
    return 0


def _capped(entries: Sequence[ListingEntry], limit: int | None) -> tuple[ListingEntry, ...]:
    if limit is None or len(entries) <= limit:
        return tuple(entries)
    LOG.info("capping this run at %d of %d discovered article(s)", limit, len(entries))
    return tuple(entries[:limit])


def _process(
    entry: ListingEntry, *, adapter: SiteAdapter, http: PoliteClient, sink: Sink
) -> ArticleOutcome:
    try:
        return _store_article(entry, adapter=adapter, http=http, sink=sink)
    except Exception:  # noqa: BLE001 — see below
        # Broad on purpose: a restyled page, a dead permalink and a rejected
        # write all fail differently and none of them is worth the rest of the
        # run. Discovery is outside this loop, so the renamed-taxonomy sentinel
        # still aborts rather than being tallied fifty times.
        LOG.warning("article failed, skipping: %s", entry.source_url, exc_info=True)
        return ArticleOutcome(stored=False)


def _store_article(
    entry: ListingEntry, *, adapter: SiteAdapter, http: PoliteClient, sink: Sink
) -> ArticleOutcome:
    data = adapter.parse_article(http.get_text(entry.source_url), entry.source_url)
    content_hash = db.article_content_hash(data)

    # The articles row goes first because its uuid namespaces every object path
    # — there is nowhere to put the pictures until it exists. Within the images
    # half the order is objects first, rows second: a crash between them leaves
    # an object nobody references, which costs a few KB of the free tier, while
    # the reverse would leave a row whose <img> 404s in the gallery.
    article_id = sink.upsert_article(data)
    sink.replace_credits(article_id, data.credits)

    batch = _store_images(data.images, article_id=article_id, http=http, sink=sink)
    sink.upsert_images(article_id, batch.rows)

    # Zero image failures or nothing. The marker is what every future run reads
    # to decide it may skip this article, so a partial one has to stay unmarked
    # and be finished next time; an article with no images at all completes here
    # too, because zero images is zero failures.
    if batch.failed:
        LOG.warning(
            "leaving %s incomplete: %d image(s) failed, so the next run picks it up again",
            entry.source_url,
            batch.failed,
        )
    else:
        sink.mark_article_complete(article_id, content_hash)

    LOG.info(
        "stored %s [%s]: %d image(s) written, %d duplicate(s), %d failed",
        entry.source_url,
        data.category,
        len(batch.rows),
        batch.skipped,
        batch.failed,
    )
    return ArticleOutcome(stored=True, images=batch)


def _store_images(
    refs: Sequence[ImageRef], *, article_id: str, http: PoliteClient, sink: Sink
) -> ImageBatch:
    rows: list[db.ImageRow] = []
    hashes: set[str] = set()
    skipped = 0
    failed = 0
    uploaded_bytes = 0

    for ref in refs:
        try:
            optimized = images.download_and_optimize(ref.source_url, http)
        except HttpError as exc:
            # Kept ahead of ImageError deliberately: both descend from
            # RuntimeError, so widening either clause would start charging an
            # outage to the wrong tally — and a CDN that is down would read as
            # a content problem for as long as it lasted.
            LOG.warning("image download failed [%s]: %s", ref.source_url, exc)
            failed += 1
            continue
        except ImageError as exc:
            LOG.warning("image unusable [%s]: %s", ref.source_url, exc)
            failed += 1
            continue

        if optimized.content_hash in hashes:
            # (article_id, content_hash) is the images table's one unique key,
            # and two rows sharing it in a single INSERT abort the batch with
            # cardinality_violation whatever `on conflict` says. Collapsing here
            # rather than in db.upsert_images also saves the upload.
            skipped += 1
            continue

        try:
            row = _upload_pair(optimized, ref=ref, article_id=article_id, sink=sink)
        except StorageError as exc:
            LOG.warning("image upload failed [%s]: %s", ref.source_url, exc)
            failed += 1
            continue

        hashes.add(optimized.content_hash)
        rows.append(row)
        uploaded_bytes += len(optimized.full) + len(optimized.thumb)

    return ImageBatch(
        rows=tuple(rows), skipped=skipped, failed=failed, bytes_uploaded=uploaded_bytes
    )


def _upload_pair(
    optimized: OptimizedImage, *, ref: ImageRef, article_id: str, sink: Sink
) -> db.ImageRow:
    """Put both derivatives in the bucket and describe the row that names them."""
    full_path = storage.object_path(article_id, optimized.content_hash)
    thumb_path = storage.object_path(article_id, optimized.content_hash, thumb=True)
    public_url = sink.upload(full_path, optimized.full)
    thumb_url = sink.upload(thumb_path, optimized.thumb)

    return db.ImageRow(
        storage_path=full_path,
        public_url=public_url,
        thumb_url=thumb_url,
        width=optimized.width,
        height=optimized.height,
        # The parser's source order, gaps and all. Renumbering around a
        # transient failure would move every later picture and reshuffle the
        # article once that image downloads; nothing requires contiguity here.
        position=ref.position,
        source_image_url=ref.source_url,
        content_hash=optimized.content_hash,
    )


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def supabase_sink(client: Client) -> Sink:
    """Bind db and storage to one Supabase client."""
    return Sink(
        completed_source_urls=lambda: db.completed_source_urls(client),
        upsert_article=lambda data: db.upsert_article(client, data),
        replace_credits=lambda article_id, credits: db.replace_credits(
            client, article_id, credits
        ),
        upload=lambda path, body: storage.upload(client, path, body),
        upsert_images=lambda article_id, rows: db.upsert_images(client, article_id, rows),
        mark_article_complete=lambda article_id, content_hash: db.mark_article_complete(
            client, article_id, content_hash
        ),
    )


def without_writes(sink: Sink) -> Sink:
    """The same reads with every write disconnected — that is --dry-run.

    Everything upstream still happens: pages are fetched, images downloaded and
    re-encoded, so a dry run reports the byte cost a real run would have added.
    """
    return Sink(
        completed_source_urls=sink.completed_source_urls,
        upsert_article=lambda data: DRY_RUN_ARTICLE_ID,
        replace_credits=lambda article_id, credits: None,
        upload=lambda path, body: f"{DRY_RUN_URL_PREFIX}{path}",
        upsert_images=lambda article_id, rows: None,
        mark_article_complete=lambda article_id, content_hash: None,
    )


def build_sink(*, dry_run: bool) -> Sink:
    """The live sink, or the same one with its writes disconnected.

    The client is built either way: reading the completed permalinks is what
    makes a dry run's article list the one a real run would process.
    """
    sink = supabase_sink(db.get_client())
    return without_writes(sink) if dry_run else sink


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return number


def _pace_seconds(value: str) -> float:
    """A pace, not just a number: zero or less is no rate limit at all.

    A bare `type=float` accepts `--min-interval -5` and hands PoliteClient a
    negative floor, which is the same as asking for every request back to back.
    Politeness is not a preference here (PHASE0_AMENDMENTS §F): GQ's robots.txt
    disallows named crawlers outright, and behaving is the whole of what keeps
    a personal archive welcome — so a typo has to stop the run, not quietly
    remove the pacing.
    """
    number = float(value)
    # `not > 0` rather than `<= 0`: NaN compares false against everything, and
    # a NaN interval would make every sleep computation undefined.
    if not number > 0:
        raise argparse.ArgumentTypeError(f"expected a positive number of seconds, got {value!r}")
    return number


def parse_args(argv: Sequence[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(
        prog="python -m gallery_scraper.pipeline",
        description="Scrape GQ Korea Style articles into Supabase.",
    )
    parser.add_argument(
        "--max-articles",
        type=_positive_int,
        default=None,
        help="stop after this many articles (backfill control; default: no cap)",
    )
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=DEFAULT_MAX_PAGES,
        help=f"listing pages to walk at most (default: {DEFAULT_MAX_PAGES})",
    )
    # There is no --full: see the module docstring. Re-scraping a range is
    # `update articles set content_hash = null where ...`, which ordinary runs
    # then work through in --max-articles-sized, resumable chunks.
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch, parse and re-encode, but write nothing to Supabase or Storage",
    )
    parser.add_argument(
        "--min-interval",
        type=_pace_seconds,
        default=DEFAULT_MIN_INTERVAL,
        help=f"seconds between requests (default: {DEFAULT_MIN_INTERVAL})",
    )
    args = parser.parse_args(argv)
    return RunConfig(
        max_articles=args.max_articles,
        max_pages=args.max_pages,
        dry_run=args.dry_run,
        min_interval=args.min_interval,
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    try:
        # One client for the whole run: discovery, articles and images share a
        # connection pool and, more importantly, one politeness budget.
        with PoliteClient(min_interval=config.min_interval) as http:
            stats = run(
                config,
                adapter=GqKoreaAdapter(http),
                http=http,
                sink=build_sink(dry_run=config.dry_run),
            )
    except Exception:  # noqa: BLE001 — the process boundary
        # Absent credentials, an unreachable Supabase, a renamed taxonomy: each
        # means this run scraped nothing, so it goes red with its traceback.
        LOG.exception("run aborted")
        return 1
    return exit_code(stats)


if __name__ == "__main__":
    sys.exit(main())
