"""Persistence tests. No network and no live Supabase: a fake PostgREST client
records the table, operation, payload, filters and conflict target of every
call, and the assertions are about what the pipeline *would have sent*.

Most of these encode a failure mode that only shows up against the real
database, by which point it is expensive:

  - two rows sharing (article_id, content_hash) in one batch abort it with
    cardinality_violation, whatever the constraint says;
  - a delete on `images` silently empties users' saved lists through the
    cascade on reactions.image_id and list_images.image_id;
  - an unpaginated select returns PostgREST's first 1000 rows with no error at
    all, and a *paginated* one that stops on a short page does the same thing
    the day the project's db-max-rows drops below our page size — either way
    the incremental scrape re-fetches the whole site forever;
  - writing images.published_date fights the trigger that owns it;
  - an upsert that omitted content_hash would leave a re-scraped article marked
    complete while its images were still being fetched, so a run that then died
    would never be retried.

Text in the fixtures is synthetic — this repo is public and GQ Korea's prose is
not ours to copy.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Callable

import pytest

from gallery_scraper.core.adapter import ArticleData, Credit, ImageRef
from gallery_scraper.db import (
    COMPLETION_COLUMN,
    ENV_SERVICE_ROLE_KEY,
    ENV_URL,
    PAGE_SIZE,
    SQL_NULL,
    ConfigError,
    DbError,
    ImageRow,
    article_content_hash,
    completed_source_urls,
    get_client,
    mark_article_complete,
    replace_credits,
    upsert_article,
    upsert_images,
)

ARTICLE_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ARTICLE_ID = "66666666-7777-8888-9999-000000000000"
CONTENT_HASH = "a" * 64


# --------------------------------------------------------------------------
# Fake PostgREST client
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Call:
    table: str
    op: str
    payload: Any = None
    on_conflict: str | None = None
    filters: tuple[tuple[str, str, Any], ...] = ()
    order: str | None = None
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class Response:
    data: list[dict[str, Any]] = field(default_factory=list)


class _Builder:
    """One chainable query. Mirrors the subset of postgrest-py we call."""

    def __init__(self, table: str, calls: list[Call], responder: Callable[[Call], list]) -> None:
        self._table = table
        self._calls = calls
        self._responder = responder
        self._op: str | None = None
        self._payload: Any = None
        self._on_conflict: str | None = None
        self._filters: list[tuple[str, str, Any]] = []
        self._order: str | None = None
        self._span: tuple[int, int] | None = None
        self._negate_next = False

    def select(self, *columns: str, **_kw: Any) -> "_Builder":
        self._op = "select"
        self._payload = columns
        return self

    def insert(self, payload: Any, **_kw: Any) -> "_Builder":
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload: Any, *, on_conflict: str = "", **_kw: Any) -> "_Builder":
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def update(self, payload: Any, **_kw: Any) -> "_Builder":
        self._op = "update"
        self._payload = payload
        return self

    def delete(self, **_kw: Any) -> "_Builder":
        self._op = "delete"
        return self

    @property
    def not_(self) -> "_Builder":
        # postgrest-py spells negation as a property that arms the *next*
        # filter, so a fake that ignored it would let `.is_(col, "null")` pass
        # for `not.is` and hide an inverted skip set.
        self._negate_next = True
        return self

    def eq(self, column: str, value: Any) -> "_Builder":
        return self._filter("eq", column, value)

    def is_(self, column: str, value: Any) -> "_Builder":
        return self._filter("is", column, value)

    def order(self, column: str, **_kw: Any) -> "_Builder":
        self._order = column
        return self

    def range(self, start: int, end: int) -> "_Builder":
        self._span = (start, end)
        return self

    def execute(self) -> Response:
        if self._op is None:
            raise AssertionError("execute() before an operation was chosen")
        call = Call(
            table=self._table,
            op=self._op,
            payload=self._payload,
            on_conflict=self._on_conflict,
            filters=tuple(self._filters),
            order=self._order,
            span=self._span,
        )
        self._calls.append(call)
        return Response(self._responder(call))

    def _filter(self, operator: str, column: str, value: Any) -> "_Builder":
        name = f"not.{operator}" if self._negate_next else operator
        self._negate_next = False
        self._filters.append((name, column, value))
        return self


class FakeClient:
    def __init__(self, responder: Callable[[Call], list] | None = None) -> None:
        self.calls: list[Call] = []
        self._responder = responder or (lambda _call: [])

    def table(self, name: str) -> _Builder:
        return _Builder(name, self.calls, self._responder)


def one_row(rows: list[dict[str, Any]]) -> Callable[[Call], list]:
    return lambda _call: rows


def calls_on(client: FakeClient, table: str) -> list[Call]:
    return [c for c in client.calls if c.table == table]


# --------------------------------------------------------------------------
# Fixtures — synthetic content, real shape
# --------------------------------------------------------------------------

def sample_article(**overrides: Any) -> ArticleData:
    base: dict[str, Any] = dict(
        source_url="https://example.test/2026/07/12/synthetic-slug/",
        category="pictorial",
        title="합성 제목 하나",
        published_date=dt.date(2026, 7, 12),
        author_name="에디터 가",
        author_url="https://example.test/author/editor-a/",
        credits=(
            Credit(role_raw="포토그래퍼", person_name="사진가 나", role="photographer"),
            Credit(
                role_raw="모델", person_name="모델 다", role="model", agency="에이전시 라"
            ),
        ),
        images=(
            ImageRef(source_url="https://example.test/img/one.jpg", position=1),
            ImageRef(source_url="https://example.test/img/two.jpg", position=2),
        ),
    )
    return ArticleData(**(base | overrides))


# Wide enough that a set anywhere in the digest's input reorders between two
# hash seeds — see test_content_hash_is_stable_across_processes.
WIDE_ARTICLE_MEMBERS = 12


def wide_article() -> ArticleData:
    """The same shape, with enough credits and images to expose set ordering."""
    return sample_article(
        credits=tuple(
            Credit(role_raw=f"역할 {n}", person_name=f"사람 {n}", role=f"role-{n}")
            for n in range(WIDE_ARTICLE_MEMBERS)
        ),
        images=tuple(
            ImageRef(source_url=f"https://example.test/img/{n}.jpg", position=n + 1)
            for n in range(WIDE_ARTICLE_MEMBERS)
        ),
    )


def image_row(**overrides: Any) -> ImageRow:
    base: dict[str, Any] = dict(
        storage_path=f"{ARTICLE_ID}/0123456789abcdef.webp",
        public_url="https://cdn.example.test/one.webp",
        thumb_url="https://cdn.example.test/one_t.webp",
        width=1600,
        height=2400,
        position=1,
        source_image_url="https://example.test/img/one.jpg",
        content_hash="0123456789abcdef" + "0" * 48,
    )
    return ImageRow(**(base | overrides))


# --------------------------------------------------------------------------
# get_client
# --------------------------------------------------------------------------

def test_get_client_names_the_missing_url(monkeypatch):
    monkeypatch.delenv(ENV_URL, raising=False)
    monkeypatch.setenv(ENV_SERVICE_ROLE_KEY, "test-key-not-a-real-secret")
    with pytest.raises(ConfigError) as excinfo:
        get_client()
    assert ENV_URL in str(excinfo.value)


def test_get_client_names_the_missing_service_role_key(monkeypatch):
    monkeypatch.setenv(ENV_URL, "https://project.supabase.test")
    monkeypatch.delenv(ENV_SERVICE_ROLE_KEY, raising=False)
    with pytest.raises(ConfigError) as excinfo:
        get_client()
    assert ENV_SERVICE_ROLE_KEY in str(excinfo.value)


def test_get_client_never_falls_back_to_an_anon_key(monkeypatch):
    # An anon-key fallback would look like it worked and then write nothing:
    # RLS denies anon on every content table. Failing loudly is the point.
    monkeypatch.setenv(ENV_URL, "https://project.supabase.test")
    monkeypatch.delenv(ENV_SERVICE_ROLE_KEY, raising=False)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key-not-a-real-secret")
    with pytest.raises(ConfigError):
        get_client()


def test_get_client_error_does_not_leak_the_key(monkeypatch):
    secret = "sbp-not-a-real-secret-value"
    monkeypatch.delenv(ENV_URL, raising=False)
    monkeypatch.setenv(ENV_SERVICE_ROLE_KEY, secret)
    with pytest.raises(ConfigError) as excinfo:
        get_client()
    message = str(excinfo.value)
    assert secret not in message
    assert secret[:6] not in message


def test_get_client_passes_the_service_role_key_to_supabase(monkeypatch):
    # create_client is patched on the supabase module itself, which is where
    # get_client() resolves it from — nothing here reaches the network.
    import supabase

    seen: dict[str, str] = {}
    monkeypatch.setattr(
        supabase,
        "create_client",
        lambda url, key: seen.update(url=url, key=key) or "client-sentinel",
    )
    monkeypatch.setenv(ENV_URL, "https://project.supabase.test")
    monkeypatch.setenv(ENV_SERVICE_ROLE_KEY, "service-key-not-a-real-secret")

    assert get_client() == "client-sentinel"
    assert seen == {
        "url": "https://project.supabase.test",
        "key": "service-key-not-a-real-secret",
    }


def test_blank_env_var_counts_as_missing(monkeypatch):
    # A key set to "" in CI is the same outage as an unset one, and an empty
    # string sails past a plain `in os.environ` check.
    monkeypatch.setenv(ENV_URL, "https://project.supabase.test")
    monkeypatch.setenv(ENV_SERVICE_ROLE_KEY, "   ")
    with pytest.raises(ConfigError):
        get_client()


# --------------------------------------------------------------------------
# article_content_hash
# --------------------------------------------------------------------------

def test_content_hash_is_a_sha256_hex_digest():
    digest = article_content_hash(sample_article())
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_content_hash_ignores_the_order_fields_were_passed_in():
    forward = ArticleData(
        source_url="https://example.test/a/",
        category="news",
        title="제목",
        author_name="에디터 가",
    )
    backward = ArticleData(
        author_name="에디터 가",
        title="제목",
        category="news",
        source_url="https://example.test/a/",
    )
    assert article_content_hash(forward) == article_content_hash(backward)


def _credit_edited(index: int, **changes: Any) -> ArticleData:
    """The sample article with one sub-field of one credit changed.

    index is 0-based into the credits tuple. The fixture's first credit carries
    no agency and its second does, so addressing either one is what makes both
    directions of the agency edit — gaining one, losing one — reachable.
    """
    edited = tuple(
        replace(credit, **changes) if position == index else credit
        for position, credit in enumerate(sample_article().credits)
    )
    return sample_article(credits=edited)


# One edit per entry, each of them something a GQ editor does on an ordinary
# Tuesday. The digest is the only record of *which version* of an article is
# stored, so a field it ignores is a field whose edit is invisible forever:
# nothing downstream can tell the row is stale, and the article is never
# re-processed. This table exists because a mutation campaign dropped category,
# published_date, author_name and author_url from the canonical form — four of
# its seven fields — and every credit sub-field independently, without turning
# a single test red.
CONTENT_EDITS: tuple[tuple[str, Callable[[], ArticleData]], ...] = (
    ("title", lambda: sample_article(title="합성 제목 둘")),
    ("category", lambda: sample_article(category="fashion")),
    ("published_date", lambda: sample_article(published_date=dt.date(2026, 7, 13))),
    # A date that disappears is an edit too, and it is the one case where the
    # canonical value is None rather than a string.
    ("published_date_cleared", lambda: sample_article(published_date=None)),
    ("author_name", lambda: sample_article(author_name="에디터 나")),
    ("author_url", lambda: sample_article(author_url="https://example.test/author/editor-b/")),
    ("credit_role_raw", lambda: _credit_edited(0, role_raw="사진")),
    ("credit_role", lambda: _credit_edited(0, role="stylist")),
    ("credit_person_name", lambda: _credit_edited(0, person_name="사진가 마")),
    # The agency suffix is the sub-field a re-parse is likeliest to gain or
    # lose, and the one whose loss looks like nothing from the outside.
    ("credit_agency", lambda: _credit_edited(0, agency="에이전시 마")),
    ("credit_agency_cleared", lambda: _credit_edited(1, agency=None)),
    (
        "credit_added",
        lambda: sample_article(
            credits=(*sample_article().credits, Credit(role_raw="스타일리스트", person_name="스타일 바"))
        ),
    ),
    ("credit_dropped", lambda: sample_article(credits=sample_article().credits[:1])),
    # Same number of pictures, different pictures: a re-shoot swapped into an
    # existing article is exactly the edit a count-only digest cannot see.
    (
        "image_swapped",
        lambda: sample_article(
            images=(
                sample_article().images[0],
                ImageRef(source_url="https://example.test/img/three.jpg", position=2),
            )
        ),
    ),
    ("image_dropped", lambda: sample_article(images=sample_article().images[:1])),
)


@pytest.mark.parametrize("edit", [pytest.param(fn, id=name) for name, fn in CONTENT_EDITS])
def test_every_covered_field_moves_the_digest(edit):
    assert article_content_hash(edit()) != article_content_hash(sample_article())


def test_no_two_different_edits_share_a_digest():
    # Stronger than the per-field check and aimed at a different mistake: a
    # canonical form that concatenates its fields (or flattens a credit into one
    # string) still moves for every edit above, while quietly making "role_raw
    # 사진, role photographer" indistinguishable from its transpose.
    edited = {article_content_hash(edit()) for _name, edit in CONTENT_EDITS}
    assert len(edited | {article_content_hash(sample_article())}) == len(CONTENT_EDITS) + 1


def test_content_hash_follows_credit_order():
    # Credits are an ordered list on the page and in the lightbox, so a
    # reshuffle is a real edit, not a no-op.
    original = sample_article()
    swapped = sample_article(credits=tuple(reversed(original.credits)))
    assert article_content_hash(original) != article_content_hash(swapped)


def test_content_hash_follows_image_order():
    # The docstring's own claim, and the one the grid and the lightbox render.
    # Sorting the URLs before hashing — or hashing a set of them — would erase
    # precisely this: a re-ordered gallery would read as untouched.
    original = sample_article()
    reordered = sample_article(images=tuple(reversed(original.images)))
    assert article_content_hash(original) != article_content_hash(reordered)


def test_a_repeated_image_is_not_the_same_article():
    # (one, two, two) and (one, two) share a set of URLs and differ as lists.
    # A photo printed twice is a layout the gallery has to reproduce, so the
    # digest has to see it — which a de-duplicating canonical form would not.
    original = sample_article()
    repeated = sample_article(images=(*original.images, original.images[-1]))
    assert article_content_hash(original) != article_content_hash(repeated)


def test_the_digest_deliberately_ignores_source_url():
    # Re-derived rather than inherited. A stored digest is only ever compared
    # against one computed for the same row, and the row is *keyed* by
    # source_url — so including it adds nothing to that comparison and adds one
    # failure mode: the day GQ normalises its permalinks (a trailing slash, a
    # slug rewrite, http to https), every digest in the table moves at once and
    # the entire archive re-scrapes. Exclusion is still right.
    assert article_content_hash(sample_article()) == article_content_hash(
        sample_article(source_url="https://example.test/2026/07/12/synthetic-slug")
    )


# A change to the digest recipe is a data migration, not a refactor: every
# stored content_hash was produced by an earlier run in another process, so
# re-ordering the canonical keys, changing the separators, dropping
# sort_keys, or reshaping a credit re-scrapes the whole archive at full cost
# and nothing anywhere reports it. The fixture below is frozen for that reason
# — it is not sample_article(), which tests are free to edit — and updating the
# expected digest is how that cost gets acknowledged out loud.
GOLDEN_ARTICLE = ArticleData(
    source_url="https://example.test/2026/07/12/golden-slug/",
    category="pictorial",
    title="고정 제목",
    published_date=dt.date(2026, 7, 12),
    author_name="에디터 가",
    author_url="https://example.test/author/editor-a/",
    credits=(
        Credit(role_raw="포토그래퍼", person_name="사진가 나", role="photographer"),
        Credit(role_raw="모델", person_name="모델 다", role="model", agency="에이전시 라"),
    ),
    images=(
        ImageRef(source_url="https://example.test/img/one.jpg", position=1),
        ImageRef(source_url="https://example.test/img/two.jpg", position=2),
    ),
)
GOLDEN_DIGEST = "3de1e4edb80edf46a2b0f65c153397ae6a2e3de5edf0bfdecb394260eae8d99d"


def test_the_digest_recipe_is_pinned():
    assert article_content_hash(GOLDEN_ARTICLE) == GOLDEN_DIGEST


# Two seeds, both explicit. The parent pytest process is *not* one of the two
# sides: its PYTHONHASHSEED is whatever the developer's shell happened to have,
# so a parent-versus-child comparison passes or fails by coin flip and a digest
# built from a set survives it most of the time.
HASH_SEEDS = ("1", "2")

_CHILD_DIGEST_SCRIPT = "import test_db as t; print(t.article_content_hash(t.wide_article()))"


def _digest_under_seed(seed: str) -> str:
    """article_content_hash(wide_article()) from a child with a pinned seed."""
    here = pathlib.Path(__file__).resolve()
    env = dict(
        os.environ,
        PYTHONHASHSEED=seed,
        PYTHONPATH=os.pathsep.join((str(here.parents[1]), str(here.parent))),
    )
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_DIGEST_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_content_hash_is_stable_across_processes():
    # A digest built from anything salted by PYTHONHASHSEED — a set, the
    # builtin hash() — would differ between the GitHub Actions run that stored
    # it and the next one, and every article would look edited on every run.
    first, second = (_digest_under_seed(seed) for seed in HASH_SEEDS)
    assert len(first) == 64
    assert first == second


def test_content_hash_matches_this_process_too():
    # The other half of the claim: the pinned-seed children agree with an
    # ordinary interpreter, so the digest is seed-independent rather than
    # merely self-consistent.
    assert _digest_under_seed(HASH_SEEDS[0]) == article_content_hash(wide_article())


# --------------------------------------------------------------------------
# completed_source_urls
# --------------------------------------------------------------------------

def paged_responder(total: int) -> Callable[[Call], list]:
    def respond(call: Call) -> list[dict[str, str]]:
        assert call.span is not None, "select without a range would truncate at 1000"
        start, end = call.span
        return [
            {"source_url": f"https://example.test/a/{i}/"}
            for i in range(start, min(end + 1, total))
        ]

    return respond


def test_completed_source_urls_pages_past_a_full_first_page():
    total = PAGE_SIZE + 3
    client = FakeClient(paged_responder(total))
    urls = completed_source_urls(client)
    assert len(urls) == total
    assert f"https://example.test/a/{total - 1}/" in urls


def test_paging_asks_for_disjoint_windows():
    total = PAGE_SIZE + 3
    client = FakeClient(paged_responder(total))
    completed_source_urls(client)
    spans = [c.span for c in calls_on(client, "articles")]
    # Three windows, not two: the short second page is not taken as proof that
    # the archive ended there — see test_a_server_that_caps_below_our_page_size.
    assert spans == [
        (0, PAGE_SIZE - 1),
        (PAGE_SIZE, 2 * PAGE_SIZE - 1),
        (total, total + PAGE_SIZE - 1),
    ]


def test_paging_is_ordered_so_windows_cannot_overlap_or_skip():
    # Offset paging over an unordered select is undefined: Postgres may return
    # a row in two windows or in none.
    client = FakeClient(paged_responder(PAGE_SIZE + 3))
    completed_source_urls(client)
    assert calls_on(client, "articles")[0].order == "source_url"


def test_the_loop_stops_at_the_first_empty_page():
    client = FakeClient(paged_responder(2))
    assert len(completed_source_urls(client)) == 2
    # Two requests: the two-row page, then the one window that comes back empty
    # and ends it. Not one (that would be trusting a short page) and not three
    # (that would be a loop with no terminal condition at all).
    assert len(calls_on(client, "articles")) == 2


def test_completed_source_urls_filters_by_site():
    client = FakeClient(paged_responder(1))
    completed_source_urls(client, site="other_site")
    assert ("eq", "source_site", "other_site") in calls_on(client, "articles")[0].filters


def test_only_articles_carrying_the_completion_marker_are_skipped():
    # The whole incremental contract: a row whose content_hash is still null is
    # a *started* article, not a finished one, and must come back from
    # discovery. Dropping this filter would strand every article whose images
    # 403'd behind a green check, forever.
    client = FakeClient(paged_responder(1))
    completed_source_urls(client)
    assert ("not.is", COMPLETION_COLUMN, SQL_NULL) in calls_on(client, "articles")[0].filters


def test_the_completion_filter_is_negated_rather_than_plain():
    # `is null` instead of `not is null` inverts the skip set: every finished
    # article would be re-scraped and every unfinished one skipped.
    client = FakeClient(paged_responder(1))
    completed_source_urls(client)
    filters = calls_on(client, "articles")[0].filters
    operators = [op for op, column, _ in filters if column == COMPLETION_COLUMN]
    assert operators == ["not.is"]


def test_null_is_sent_as_postgrest_spells_it():
    # PostgREST parses the literal string "null" inside an `is` filter; a
    # Python None would be serialised as the word "None" and match no row.
    client = FakeClient(paged_responder(1))
    completed_source_urls(client)
    filters = calls_on(client, "articles")[0].filters
    values = [value for _op, column, value in filters if column == COMPLETION_COLUMN]
    assert values == ["null"]


def test_completed_source_urls_unions_repeats_across_pages():
    # Defensive: if paging ever hands back an overlapping window, the caller
    # must still see a set of distinct URLs.
    def respond(call: Call) -> list[dict[str, str]]:
        start, _end = call.span
        if start == 0:
            return [{"source_url": f"https://example.test/a/{i}/"} for i in range(PAGE_SIZE)]
        if start == PAGE_SIZE:
            return [{"source_url": "https://example.test/a/0/"}]  # already seen
        return []

    client = FakeClient(respond)
    assert len(completed_source_urls(client)) == PAGE_SIZE


def test_rows_without_a_usable_source_url_are_dropped():
    # source_url is NOT NULL in the schema, so none of these can come from a
    # healthy response — which is the point. A None in the skip set can never
    # match a discovered URL, and it detonates on the first sort or join anyone
    # writes over `seen`, a long way from the response that produced it.
    def respond(call: Call) -> list[dict[str, Any]]:
        if call.span[0] > 0:
            return []
        return [
            {"source_url": "https://example.test/a/0/"},
            {"source_url": None},
            {"source_url": ""},
            {},
        ]

    client = FakeClient(respond)
    assert completed_source_urls(client) == {"https://example.test/a/0/"}


# A project whose db-max-rows sits below our PAGE_SIZE — the only relationship
# between the two numbers these tests depend on. PostgREST truncates to the cap
# silently: no error, no header this code reads, just a short page.
SERVER_ROW_CAP = 400
# Enough rows to need three capped windows, expressed in the cap rather than in
# PAGE_SIZE so that re-tuning the page size cannot turn a passing run red. That
# independence is the whole claim: PAGE_SIZE is a knob now, not a contract.
CAPPED_TOTAL = 2 * SERVER_ROW_CAP + 1


def capped_responder(total: int, cap: int) -> Callable[[Call], list]:
    """A server that returns at most `cap` rows however wide a window we ask for."""

    def respond(call: Call) -> list[dict[str, str]]:
        assert call.span is not None, "select without a range would truncate at 1000"
        start, end = call.span
        wanted = list(range(start, min(end + 1, total)))
        return [{"source_url": f"https://example.test/a/{i}/"} for i in wanted[:cap]]

    return respond


def test_a_server_that_caps_below_our_page_size_still_yields_every_url():
    # The failure this replaces was silent and permanent: a first window that
    # comes back short because of the server's cap, read as "the archive ends
    # here", returns a partial skip set — so every article past the cap looks
    # unseen and is re-fetched, re-encoded and re-uploaded on every single run.
    client = FakeClient(capped_responder(CAPPED_TOTAL, SERVER_ROW_CAP))
    urls = completed_source_urls(client)
    assert len(urls) == CAPPED_TOTAL
    assert f"https://example.test/a/{CAPPED_TOTAL - 1}/" in urls


def test_windows_advance_by_the_rows_actually_returned():
    # The mechanism behind the test above: the next offset is where this page
    # really ended, not where PAGE_SIZE says it should have. Advancing by
    # PAGE_SIZE against a capping server steps straight over the rows it held
    # back, leaving holes that no later window ever revisits.
    client = FakeClient(capped_responder(CAPPED_TOTAL, SERVER_ROW_CAP))
    completed_source_urls(client)
    starts = [c.span[0] for c in calls_on(client, "articles")]
    assert starts == [0, SERVER_ROW_CAP, 2 * SERVER_ROW_CAP, CAPPED_TOTAL]


# --------------------------------------------------------------------------
# upsert_article
# --------------------------------------------------------------------------

def test_upsert_article_returns_the_row_id():
    client = FakeClient(one_row([{"id": ARTICLE_ID}]))
    assert upsert_article(client, sample_article()) == ARTICLE_ID


def test_upsert_article_arbitrates_on_source_url():
    client = FakeClient(one_row([{"id": ARTICLE_ID}]))
    upsert_article(client, sample_article())
    assert calls_on(client, "articles")[0].on_conflict == "source_url"


def test_upsert_article_sends_the_parsed_fields():
    client = FakeClient(one_row([{"id": ARTICLE_ID}]))
    data = sample_article()
    upsert_article(client, data)
    payload = calls_on(client, "articles")[0].payload
    assert payload["source_url"] == data.source_url
    assert payload["category"] == "pictorial"
    assert payload["title"] == data.title
    assert payload["author_name"] == data.author_name
    assert payload["author_url"] == data.author_url


def test_upsert_article_writes_the_completion_marker_null():
    # Not omitted — written. On the conflict path an omitted column keeps its
    # old value, which would leave a re-scraped article marked complete while
    # its images were still being fetched: a run killed in between would then
    # never be retried.
    client = FakeClient(one_row([{"id": ARTICLE_ID}]))
    upsert_article(client, sample_article())
    payload = calls_on(client, "articles")[0].payload
    assert COMPLETION_COLUMN in payload
    assert payload[COMPLETION_COLUMN] is None


def test_dates_are_serialized_as_iso_strings():
    # json.dumps cannot encode datetime.date, so a raw date would blow up
    # inside the transport rather than here.
    client = FakeClient(one_row([{"id": ARTICLE_ID}]))
    upsert_article(client, sample_article())
    assert calls_on(client, "articles")[0].payload["published_date"] == "2026-07-12"


def test_a_dateless_article_sends_null_not_a_string():
    client = FakeClient(one_row([{"id": ARTICLE_ID}]))
    upsert_article(client, sample_article(published_date=None))
    assert calls_on(client, "articles")[0].payload["published_date"] is None


def test_upsert_article_refreshes_the_timestamps():
    client = FakeClient(one_row([{"id": ARTICLE_ID}]))
    upsert_article(client, sample_article())
    payload = calls_on(client, "articles")[0].payload
    assert payload["updated_at"] and payload["scraped_at"]
    assert dt.datetime.fromisoformat(payload["updated_at"]).tzinfo is not None


def test_upsert_article_raises_when_no_row_comes_back():
    # An upsert that wrote nothing still has to stop the article here: carrying
    # on would hand every downstream write a null article_id.
    client = FakeClient(one_row([]))
    with pytest.raises(DbError):
        upsert_article(client, sample_article())


def test_upsert_article_raises_when_the_row_carries_no_id():
    # A row can come back without the column — a changed Prefer header, a
    # narrowed select — and it is not a success: the id namespaces the storage
    # paths and owns every credit and image written after it.
    client = FakeClient(one_row([{"source_url": sample_article().source_url}]))
    with pytest.raises(DbError):
        upsert_article(client, sample_article())


def test_upsert_article_refuses_a_multi_row_response():
    # One dict goes in, so more than one row coming back means the response is
    # not the one this contract describes. The returned id namespaces the
    # storage paths and owns the credits and images written straight after it,
    # so picking an arbitrary row out of the pile would file a whole article
    # under a sibling — invisible until someone opened the gallery. Inert while
    # the payload stays a single dict, which is exactly why it is pinned before
    # a backfill makes it a batch.
    client = FakeClient(one_row([{"id": ARTICLE_ID}, {"id": OTHER_ARTICLE_ID}]))
    with pytest.raises(DbError) as excinfo:
        upsert_article(client, sample_article())
    assert sample_article().source_url in str(excinfo.value)


# --------------------------------------------------------------------------
# mark_article_complete
# --------------------------------------------------------------------------

def test_marking_complete_updates_only_the_completion_marker():
    # Any other column here would fight either the upsert that just wrote it or
    # the articles_set_updated_at trigger that owns updated_at.
    client = FakeClient()
    mark_article_complete(client, ARTICLE_ID, CONTENT_HASH)
    call = calls_on(client, "articles")[0]
    assert call.op == "update"
    assert call.payload == {COMPLETION_COLUMN: CONTENT_HASH}


def test_marking_complete_is_scoped_to_one_article():
    # An update without the filter would stamp this hash on every article in
    # the table and make the entire archive look complete.
    client = FakeClient()
    mark_article_complete(client, ARTICLE_ID, CONTENT_HASH)
    assert calls_on(client, "articles")[0].filters == (("eq", "id", ARTICLE_ID),)


def test_an_empty_marker_is_refused_before_it_reaches_the_table():
    # A blank string is not null, so it would read as complete on every future
    # run while saying nothing about which version was stored.
    client = FakeClient()
    with pytest.raises(DbError) as excinfo:
        mark_article_complete(client, ARTICLE_ID, "")
    assert ARTICLE_ID in str(excinfo.value)
    assert client.calls == []


# --------------------------------------------------------------------------
# replace_credits
# --------------------------------------------------------------------------

def test_credits_are_deleted_then_reinserted():
    client = FakeClient()
    replace_credits(client, ARTICLE_ID, sample_article().credits)
    ops = [c.op for c in calls_on(client, "article_credits")]
    assert ops == ["delete", "insert"]


def test_the_delete_is_scoped_to_one_article():
    client = FakeClient()
    replace_credits(client, ARTICLE_ID, sample_article().credits)
    delete = calls_on(client, "article_credits")[0]
    assert delete.filters == (("eq", "article_id", ARTICLE_ID),)


def test_credit_positions_are_one_based_and_contiguous():
    client = FakeClient()
    credits = sample_article().credits
    replace_credits(client, ARTICLE_ID, credits)
    payload = calls_on(client, "article_credits")[1].payload
    assert [row["position"] for row in payload] == list(range(1, len(credits) + 1))


def test_credit_payload_carries_the_parsed_fields():
    client = FakeClient()
    replace_credits(client, ARTICLE_ID, sample_article().credits)
    model = calls_on(client, "article_credits")[1].payload[1]
    assert model == {
        "article_id": ARTICLE_ID,
        "position": 2,
        "role_raw": "모델",
        "role": "model",
        "person_name": "모델 다",
        "agency": "에이전시 라",
    }


def test_an_article_that_lost_its_credits_still_gets_the_delete():
    client = FakeClient()
    replace_credits(client, ARTICLE_ID, ())
    ops = [c.op for c in calls_on(client, "article_credits")]
    assert ops == ["delete"]


# --------------------------------------------------------------------------
# upsert_images
# --------------------------------------------------------------------------

def test_duplicate_content_hashes_collapse_before_the_batch_is_sent():
    # Same bytes twice in one body. Sending both rows aborts the INSERT with
    # cardinality_violation no matter what the unique constraint says.
    client = FakeClient()
    rows = (
        image_row(position=1),
        image_row(position=4, source_image_url="https://example.test/img/one-copy.jpg"),
    )
    upsert_images(client, ARTICLE_ID, rows)
    payload = calls_on(client, "images")[0].payload
    assert len(payload) == 1
    assert payload[0]["position"] == 1  # first occurrence wins


def test_distinct_hashes_are_all_kept():
    client = FakeClient()
    rows = (image_row(), image_row(position=2, content_hash="f" * 64))
    upsert_images(client, ARTICLE_ID, rows)
    assert len(calls_on(client, "images")[0].payload) == 2


def test_images_arbitrate_on_the_only_unique_constraint():
    client = FakeClient()
    upsert_images(client, ARTICLE_ID, (image_row(),))
    assert calls_on(client, "images")[0].on_conflict == "article_id,content_hash"


def test_published_date_is_left_to_the_trigger():
    client = FakeClient()
    upsert_images(client, ARTICLE_ID, (image_row(),))
    assert "published_date" not in calls_on(client, "images")[0].payload[0]


def test_images_are_never_deleted():
    # reactions.image_id and list_images.image_id cascade off images.id, so a
    # delete-and-recreate reconciliation drops images out of saved lists.
    client = FakeClient()
    upsert_images(client, ARTICLE_ID, (image_row(),))
    assert [c.op for c in calls_on(client, "images")] == ["upsert"]


def test_image_payload_matches_the_columns():
    client = FakeClient()
    row = image_row()
    upsert_images(client, ARTICLE_ID, (row,))
    assert calls_on(client, "images")[0].payload[0] == {
        "article_id": ARTICLE_ID,
        "storage_path": row.storage_path,
        "public_url": row.public_url,
        "thumb_url": row.thumb_url,
        "width": row.width,
        "height": row.height,
        "position": row.position,
        "source_image_url": row.source_image_url,
        "content_hash": row.content_hash,
    }


def test_no_rows_means_no_request():
    client = FakeClient()
    upsert_images(client, ARTICLE_ID, ())
    assert calls_on(client, "images") == []


@pytest.mark.parametrize("field_name,bad", [("width", 0), ("height", -1), ("position", 0)])
def test_image_row_rejects_values_the_check_constraints_would(field_name, bad):
    with pytest.raises(ValueError) as excinfo:
        image_row(**{field_name: bad})
    assert field_name in str(excinfo.value)


@pytest.mark.parametrize("field_name", ["storage_path", "public_url", "thumb_url", "content_hash"])
def test_image_row_rejects_blank_not_null_columns(field_name):
    with pytest.raises(ValueError) as excinfo:
        image_row(**{field_name: ""})
    assert field_name in str(excinfo.value)
