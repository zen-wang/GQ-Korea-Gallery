"""Supabase writes for the scraped content tables (articles, article_credits,
images) via supabase-py.

Three rules from the schema shape everything below, and each of them fails in a
way that is invisible from here (see supabase/README.md §Decisions worth
knowing):

  - `images` carries exactly one unique constraint, `(article_id, content_hash)`,
    because `on conflict` can arbitrate exactly one. Duplicates therefore have
    to be collapsed *in memory* before a batch is sent: two rows with the same
    key in one INSERT abort it with cardinality_violation whatever the
    constraint says.
  - `images` rows are updated, never deleted. reactions.image_id and
    list_images.image_id cascade off images.id, so delete-and-recreate
    reconciliation would drop pictures out of people's saved lists silently.
  - `images.published_date` is denormalised and maintained by trigger. Writing
    it from here would fight the trigger and eventually disagree with it.

One column carries more than its name suggests. `articles.content_hash` is
also the run's *completion marker*: upsert_article writes it NULL and only
mark_article_complete fills it in, once every image of that article has landed.
So "we already have this one" is completed_source_urls, not "a row exists" — an
article whose images all 403'd stays out of the skip set and is retried by the
next run instead of sitting imageless behind a green check.

supabase is imported lazily inside get_client() so the package imports without
it; every other function takes a client, which also keeps them testable without
the network.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from gallery_scraper.core.adapter import ArticleData, Credit

if TYPE_CHECKING:  # supabase stays out of the import path at runtime
    from supabase import Client

ENV_URL = "SUPABASE_URL"
ENV_SERVICE_ROLE_KEY = "SUPABASE_SERVICE_ROLE_KEY"

DEFAULT_SITE = "gq_korea"

ARTICLES = "articles"
CREDITS = "article_credits"
IMAGES = "images"

ARTICLES_CONFLICT_TARGET = "source_url"
IMAGES_CONFLICT_TARGET = "article_id,content_hash"

# The completion marker, named because three functions have to agree on it.
COMPLETION_COLUMN = "content_hash"
# PostgREST spells SQL NULL as the literal string "null" inside an `is` filter;
# a Python None would be serialised as the word "None" and match nothing.
SQL_NULL = "null"

# PostgREST caps a response at db-max-rows — 1000 on a default project — and
# says nothing when it truncates. A single unpaginated select would quietly
# return the first page forever, so completed_source_urls would never see the
# tail of the archive and every run would re-scrape it.
#
# This is a performance knob, not a correctness one: the paging loop below
# terminates on an empty page and advances by what actually came back, so a
# project whose cap sits below this number costs extra round trips and nothing
# else. Raising it above the server's cap does not truncate the skip set.
PAGE_SIZE = 1000

# Columns the schema declares NOT NULL, checked at construction so a bad row
# fails on the machine that built it rather than aborting a whole batch.
_IMAGE_TEXT_COLUMNS = (
    "storage_path",
    "public_url",
    "thumb_url",
    "source_image_url",
    "content_hash",
)


class DbError(RuntimeError):
    """A write did not produce the result the pipeline needs to continue."""


class ConfigError(DbError):
    """Required configuration is absent from the environment."""


@dataclass(frozen=True)
class ImageRow:
    """One row of `images`, already uploaded and measured.

    content_hash is the sha256 of the *source* bytes, not of the WebP we store:
    it has to stay stable if the encoder settings ever change, so a re-scrape
    keeps matching existing rows instead of re-uploading the entire archive.
    """

    storage_path: str
    public_url: str
    thumb_url: str
    width: int
    height: int
    position: int
    source_image_url: str
    content_hash: str

    def __post_init__(self) -> None:
        for column in _IMAGE_TEXT_COLUMNS:
            if not str(getattr(self, column)).strip():
                raise ValueError(f"{column} is NOT NULL in images and cannot be blank")
        for column in ("width", "height"):
            if getattr(self, column) <= 0:
                raise ValueError(f"{column} must be positive — images checks {column} > 0")
        if self.position < 1:
            raise ValueError("position is 1-based within the article body")


def get_client() -> Client:
    """Service-role client, built from the environment.

    There is deliberately no anon-key fallback. The anon role is denied every
    content table by RLS, so a fallback client would connect happily and then
    write nothing — a silent no-op scrape is far worse than a startup crash.
    """
    url = _required_env(ENV_URL)
    key = _required_env(ENV_SERVICE_ROLE_KEY)

    from supabase import create_client

    return create_client(url, key)


def article_content_hash(data: ArticleData) -> str:
    """Stable sha256 over the parsed article, for cheap edit detection.

    source_url is excluded on purpose: it is the row's identity, not its
    content, and hashing it would make every article look changed if GQ ever
    normalised its permalinks.

    Stability is the whole value here — the digest is compared against one
    computed by a previous CI run in a different process. So: JSON with sorted
    keys (field order at the call site cannot matter), no sets and no builtin
    hash() anywhere (both are salted by PYTHONHASHSEED), and dates as ISO
    strings rather than repr(). Credits and images stay ordered, because their
    order is what the lightbox and the grid render.
    """
    canonical: dict[str, Any] = {
        "title": data.title,
        "category": data.category,
        "published_date": _iso_date(data.published_date),
        "author_name": data.author_name,
        "author_url": data.author_url,
        "credits": [[c.role_raw, c.role, c.person_name, c.agency] for c in data.credits],
        "images": [image.source_url for image in data.images],
    }
    serialized = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def completed_source_urls(client: Client, *, site: str = DEFAULT_SITE) -> set[str]:
    """Every source_url this site has *finished*, for discovery's skip set.

    Finished means content_hash is not null — see the module docstring. A row
    on its own only says a run started on that article; an article whose images
    failed is deliberately absent here so the next run picks it up again.

    Ordered by source_url — it is unique, so it gives the windows a total order.
    Offset paging over an unordered select is undefined in Postgres: a row can
    surface in two windows or in none.

    Only an empty page ends the loop, and the window advances by the number of
    rows that actually came back. The obvious "a short page is the last page"
    test would tie correctness to PAGE_SIZE matching the server's db-max-rows,
    which we neither control nor can read: the day a project's cap sits below
    PAGE_SIZE, the first window comes back short, this returns a *partial* skip
    set, and every article past it is re-scraped at full cost on every run,
    silently. The price of not trusting the cap is one extra request per run.
    """
    urls: set[str] = set()
    offset = 0
    while True:
        response = (
            client.table(ARTICLES)
            .select("source_url")
            .eq("source_site", site)
            .not_.is_(COMPLETION_COLUMN, SQL_NULL)
            .order("source_url")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        page = response.data or []
        if not page:
            return urls
        # source_url is NOT NULL in the schema, so a missing or blank one means
        # the response is not the one we asked for. Such a row is dropped rather
        # than carried: a falsy member can never match a discovered URL, and a
        # None inside a set of str detonates on the first sort or join anyone
        # downstream writes, a long way from the response that caused it.
        urls = urls | {row["source_url"] for row in page if row.get("source_url")}
        offset += len(page)


def upsert_article(client: Client, data: ArticleData) -> str:
    """Insert or update one article on the source_url unique key; return its id.

    content_hash is written NULL on both paths, deliberately: it is the
    completion marker, and an article is incomplete until its images are in the
    bucket. A re-scrape of a stored article therefore drops back to incomplete
    until mark_article_complete says otherwise — the safe direction, because the
    cost of re-processing an article is one fetch, and the cost of the opposite
    mistake is a permanently imageless row that no future run ever looks at.

    source_site is left to the column default: ArticleData carries no site, and
    the adapter that produced it is the site. A second adapter is what turns
    this into a keyword argument.

    Both timestamps are written explicitly. articles_set_updated_at refreshes
    updated_at on the conflict path, but nothing refreshes scraped_at, and
    "when did we last look at this article" has to stay true on a re-scrape
    that changed nothing.
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    payload = {
        "source_url": data.source_url,
        "category": data.category,
        "title": data.title,
        # PostgREST speaks JSON, which has no date type; a datetime.date would
        # raise inside the transport instead of here.
        "published_date": _iso_date(data.published_date),
        "author_name": data.author_name,
        "author_url": data.author_url,
        # Not omitted — written. On the conflict path an omitted column keeps
        # its old value, which would leave a re-scraped article marked complete
        # while its images are still being fetched.
        COMPLETION_COLUMN: None,
        "scraped_at": now,
        "updated_at": now,
    }

    response = (
        client.table(ARTICLES)
        .upsert(payload, on_conflict=ARTICLES_CONFLICT_TARGET)
        .execute()
    )
    rows = response.data or []
    # One dict in, one row out. The count is checked rather than assumed because
    # the contract is "the id of *this* article": the returned id namespaces the
    # storage paths and owns the credits and images written next, so picking one
    # row out of several would file a whole article under a sibling. Guarding it
    # here is also what makes rows[0] safe to write — with the count pinned at
    # one there is no other row to choose, and the day someone batches this
    # payload for a backfill they get a loud failure instead of scrambled rows.
    if len(rows) != 1:
        raise DbError(
            f"upsert returned {len(rows)} rows for {data.source_url}, expected exactly one"
        )
    article_id = rows[0].get("id")
    if not article_id:
        raise DbError(f"upsert returned no article id for {data.source_url}")
    return str(article_id)


def mark_article_complete(client: Client, article_id: str, content_hash: str) -> None:
    """Stamp the completion marker on an article whose images all landed.

    The pipeline calls this only after a zero-failure image batch, which is what
    lets completed_source_urls treat the marker as "nothing left to do here".

    updated_at is left alone: articles_set_updated_at fires on this UPDATE like
    any other, and this *is* a change to the row.
    """
    if not content_hash:
        # A blank marker would read as complete on the next run while telling
        # us nothing about which version of the article was stored.
        raise DbError(f"refusing to mark {article_id} complete with an empty content hash")

    (
        client.table(ARTICLES)
        .update({COMPLETION_COLUMN: content_hash})
        .eq("id", article_id)
        .execute()
    )


def replace_credits(client: Client, article_id: str, credits: Sequence[Credit]) -> None:
    """Replace an article's credits wholesale, positions 1-based and contiguous.

    Safe here and nowhere else: nothing references article_credits.id, so no
    user data hangs off the rows being dropped. `unique (article_id, position)`
    exists precisely because this table is replaced rather than merged.

    The delete and the insert are two PostgREST calls, not one transaction, so
    a crash between them leaves an article credit-less until the next run
    rewrites it — and that last clause is only true because of the completion
    marker. upsert_article nulls content_hash before this function runs, so an
    article interrupted here is not in completed_source_urls and is discovered
    again. Under the old "a row exists means done" rule it was false: the
    credit-less article was skipped forever. The alternative — merging on
    position — would leave stale credits behind when an article loses one,
    which no run would ever repair.

    Credit carries no position of its own: source order is the position, and
    renumbering from 1 here keeps the sequence contiguous after the parser has
    dropped sponsor rows.
    """
    client.table(CREDITS).delete().eq("article_id", article_id).execute()
    if not credits:
        return

    payload = [
        {
            "article_id": article_id,
            "position": position,
            "role_raw": credit.role_raw,
            "role": credit.role,
            "person_name": credit.person_name,
            "agency": credit.agency,
        }
        for position, credit in enumerate(credits, start=1)
    ]
    client.table(CREDITS).insert(payload).execute()


def upsert_images(client: Client, article_id: str, rows: Sequence[ImageRow]) -> None:
    """Insert or update an article's images. Never deletes any.

    See the module docstring: the cascade off images.id means a delete here
    reaches into users' saved lists, and duplicate content hashes have to be
    gone before the batch leaves this process.
    """
    payload = [
        {"article_id": article_id, **_image_payload(row)}
        for row in _collapse_by_content_hash(rows)
    ]
    if not payload:
        return

    client.table(IMAGES).upsert(payload, on_conflict=IMAGES_CONFLICT_TARGET).execute()


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------

def _required_env(name: str) -> str:
    """Read one required variable, naming it — and only it — when it is absent.

    Never the value or any prefix of it: this reads a service-role key, and CI
    logs are forever.
    """
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is missing or empty in the environment")
    return value


def _iso_date(value: dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _collapse_by_content_hash(rows: Sequence[ImageRow]) -> tuple[ImageRow, ...]:
    """First occurrence of each content_hash, in order.

    First rather than last because position is the article's reading order, and
    the earliest appearance is where a repeated picture belongs.
    """
    first_by_hash: dict[str, ImageRow] = {}
    for row in rows:
        first_by_hash.setdefault(row.content_hash, row)
    return tuple(first_by_hash.values())


def _image_payload(row: ImageRow) -> dict[str, Any]:
    """One images row, minus published_date — the trigger owns that column."""
    return {
        "storage_path": row.storage_path,
        "public_url": row.public_url,
        "thumb_url": row.thumb_url,
        "width": row.width,
        "height": row.height,
        "position": row.position,
        "source_image_url": row.source_image_url,
        "content_hash": row.content_hash,
    }
