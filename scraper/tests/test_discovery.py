"""Discovery tests for the GQ Korea adapter.

Discovery goes through `admin-ajax.php`, the one `/wp-admin/` path robots.txt
explicitly allows. The endpoint's contract was probed live on 2026-08-23 and is
written up in scraper/README.md; the behaviours that are non-obvious enough to
regress silently are each pinned by a test here:

  - `notInPosts` must be sent non-empty or the response has no posts at all
  - end-of-list reads as the `current_posts` key being *absent*, not empty
  - the same response on page 1 means discovery has gone blind, never the end
  - `current_term.tax1_term == false` is the renamed-taxonomy sentinel
  - `post_terms` can be the parent `STYLE`, which is not a valid article_category

The incremental stop rule is pinned here too: `seen` skips an entry, and only a
page that is *entirely* seen ends the walk. Stopping at the first seen entry is
what strands a capped or timed-out run's leftovers forever. "Entirely seen" is
also not "entirely already collected": a page replayed by offset drift is unseen
work we happen to be holding, not a page that is finished with.

tests/fixtures/listing_style_page1.json mirrors a real response *shape* with
synthetic content: invented titles, invented editor names, invented slugs. This
repo is public, so the publication's own copy is not reproduced in it — the same
policy the header of tests/fixtures/article_pictorial.html records. Parsing only
cares about structure, so nothing is lost by substituting the text.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from gallery_scraper.core.adapter import ListingEntry
from gallery_scraper.sites.gq_korea import GqKoreaAdapter, parse_listing_page

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# The percent-encoded Hangul permalink is the canonical articles.source_url, so
# it has to survive parsing byte for byte — decoding it would produce a URL that
# no longer matches the row we already stored.
PICTORIAL_URL = (
    "https://www.gqkorea.co.kr/2026/08/21/"
    "%ec%97%ac%eb%a6%84%ec%9d%98-%eb%81%9d%ec%97%90%ec%84%9c-%ea%b2%80%ec%a0%95/"
)

TAX1_TERM = {"term_id": 56, "name": "STYLE", "slug": "style", "count": 7452}

# "the key is not in the object at all", which no literal value can stand in for.
NO_KEY = object()


@pytest.fixture(scope="module")
def page1() -> dict:
    return json.loads((FIXTURES / "listing_style_page1.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def entries(page1) -> tuple[ListingEntry, ...]:
    return parse_listing_page(page1)


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

def make_post(post_id: str, *, category: str = "item", date: str = "2026.08.20",
              permalink: str | None = None) -> dict:
    """One synthetic post object in the endpoint's shape."""
    return {
        "permalink": permalink or f"https://www.gqkorea.co.kr/2026/08/20/post-{post_id}/",
        "post_date": date,
        "post_id": post_id,
        "post_title": f"테스트 글 {post_id}",
        "post_terms": category,
        "post_editors": "김에디터",
        "post_thumbnail_url": f"https://img.gqkorea.co.kr/gq/2026/08/style_{post_id}-500x500.jpg",
    }


def make_page(*posts: dict) -> dict:
    return {"current_term": {"tax1_term": TAX1_TERM}, "current_posts": list(posts)}


# What the endpoint actually returns past the last page: current_term only.
END_OF_LIST = {"current_term": {"tax1_term": TAX1_TERM}}


class FakeClient:
    """Records requests and replays canned pages, keyed by the `paged` field.

    Only post_json is implemented: discovery never GETs, and mirroring more of
    PoliteClient's surface would be a second implementation to keep in sync.
    """

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post_json(self, url: str, data):
        self.calls.append((url, dict(data)))
        index = int(data["paged"]) - 1
        return self.pages[index] if index < len(self.pages) else END_OF_LIST

    @property
    def paged_requested(self) -> list[str]:
        return [body["paged"] for _, body in self.calls]


# --------------------------------------------------------------------------
# parse_listing_page — categories
# --------------------------------------------------------------------------

def test_the_five_subcategories_are_accepted(entries):
    assert [e.category for e in entries] == [
        "pictorial",
        "item",
        "news",
        "grooming",
        "sneakers",
    ]


def test_parent_only_style_row_is_skipped(entries):
    # post_terms == "STYLE" means the article is filed under the parent with no
    # subcategory. article_category is an enum of the five lowercase children,
    # so letting one through would fail the insert with invalid_text_representation.
    assert all(e.post_id != "500006" for e in entries)
    assert all(e.category != "style" for e in entries)


def test_unknown_term_is_skipped_rather_than_raising(entries):
    # A new subcategory should cost us one missed article until someone extends
    # CATEGORIES — never a crashed run over the whole page.
    assert all(e.post_id != "500007" for e in entries)


# --------------------------------------------------------------------------
# parse_listing_page — fields
# --------------------------------------------------------------------------

def test_percent_encoded_permalink_is_preserved_verbatim(entries):
    assert entries[0].source_url == PICTORIAL_URL


def test_post_id_stays_a_string(entries):
    # The endpoint sends it as a string; coercing to int here would only invite
    # a type mismatch downstream.
    assert entries[0].post_id == "500001"


def test_title_is_carried_through(entries):
    assert entries[0].title == "여름의 끝에서, 검정"


def test_listing_date_is_parsed_as_a_date(entries):
    # "YYYY.MM.DD", already the KST display date — unlike the article page's
    # meta timestamp, there is no timezone conversion to do here.
    assert entries[0].published_date == dt.date(2026, 8, 21)


def test_malformed_date_degrades_to_none_without_dropping_the_entry(entries):
    news = next(e for e in entries if e.post_id == "500003")
    assert news.published_date is None
    assert news.source_url.endswith("/weekly-news-brief/")


def test_missing_optional_fields_do_not_kill_the_entry(entries):
    # This post carries no post_editors and no post_thumbnail_url. Discovery
    # reads neither — the article page is authoritative for author and images —
    # so their absence must not raise.
    grooming = next(e for e in entries if e.post_id == "500004")
    assert grooming.category == "grooming"
    assert grooming.published_date == dt.date(2026, 8, 18)


def test_entries_are_frozen(entries):
    with pytest.raises(Exception):
        entries[0].category = "news"  # type: ignore[misc]


# --------------------------------------------------------------------------
# parse_listing_page — terminal conditions and the taxonomy sentinel
# --------------------------------------------------------------------------

def test_missing_current_posts_is_end_of_list():
    # The site's own JS reads data['current_posts'].length unguarded and would
    # throw here; absent and empty have to mean the same thing to us.
    assert parse_listing_page(END_OF_LIST) == ()


def test_empty_current_posts_is_end_of_list():
    assert parse_listing_page(make_page()) == ()


def test_current_posts_of_the_wrong_type_raises():
    # Nothing on the far side is schema-checked. If current_posts ever comes
    # back as an object keyed by id, iterating it would yield strings and every
    # row would silently filter out as "not a Mapping" — zero articles, no error.
    payload = {"current_term": {"tax1_term": TAX1_TERM}, "current_posts": {"1": make_post("1")}}
    with pytest.raises(ValueError, match="current_posts"):
        parse_listing_page(payload)


def test_a_row_without_a_permalink_is_skipped():
    # The permalink is the identity: it is articles.source_url and the only
    # thing to fetch. A row without one cannot be stored or re-found, and
    # letting it through would put an empty source_url into the crawl.
    blank = {**make_post("1"), "permalink": ""}
    entries = parse_listing_page(make_page(blank, make_post("2")))

    assert [e.post_id for e in entries] == ["2"]


@pytest.mark.parametrize("payload", [[], [{"post_id": "500001"}], "current_posts", 0, None])
def test_a_payload_that_is_not_a_json_object_raises(payload):
    # post_json hands back whatever json.loads produced, and a top-level array
    # or scalar really does arrive: a PHP notice printed before the JSON, a WAF
    # challenge, a handler that starts returning a bare list. Reaching .get() on
    # one would be an AttributeError naming nothing anybody can act on.
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_listing_page(payload)


def test_a_row_that_is_not_an_object_is_skipped_rather_than_raising():
    # Same reasoning as the unknown-subcategory skip: one malformed row must not
    # cost the whole page. current_posts carrying bare ids is exactly the shape
    # change test_current_posts_of_the_wrong_type_raises guards the other end of.
    payload = {
        "current_term": {"tax1_term": TAX1_TERM},
        "current_posts": ["500001", None, 7, make_post("1")],
    }

    assert [e.post_id for e in parse_listing_page(payload)] == ["1"]


def test_a_null_field_becomes_an_empty_string_not_the_word_none():
    # Every field goes through the same coercion because nothing on the far side
    # is schema-checked. A JSON null must not reach articles.post_id as "None" —
    # a four-character string that looks like data and matches nothing.
    entries = parse_listing_page(make_page({**make_post("1"), "post_id": None}))

    assert entries[0].post_id == ""


@pytest.mark.parametrize("post_date", [NO_KEY, None, "", "   "])
def test_an_absent_or_blank_post_date_leaves_published_date_none(post_date):
    # Distinct from the malformed-format case above: here there is nothing to
    # parse at all. Both degrade to None, and neither costs us the article — the
    # article page carries an authoritative timestamp.
    post = make_post("1")
    post = (
        {k: v for k, v in post.items() if k != "post_date"}
        if post_date is NO_KEY
        else {**post, "post_date": post_date}
    )
    entries = parse_listing_page(make_page(post))

    assert entries[0].published_date is None
    assert entries[0].post_id == "1", "a dateless post is still worth fetching"


def test_tax1_term_false_raises_and_names_the_cause():
    # A renamed taxonomy would otherwise read as "0 new articles" forever.
    payload = {"current_term": {"tax1_term": False}}
    with pytest.raises(ValueError, match="tax1_slug"):
        parse_listing_page(payload)


# --------------------------------------------------------------------------
# discover_article_urls — the request body
# --------------------------------------------------------------------------

def test_driver_always_sends_notinposts_non_empty():
    # Non-obvious and easy for a refactor to drop: with notInPosts absent or
    # empty the response omits current_posts entirely, so discovery would go
    # quietly blind. 0 is not a real post id, so nothing is excluded — the
    # site's own JS puts 17 "recommended" ids here and skips them.
    client = FakeClient([make_page(make_post("1")), make_page(make_post("2"))])
    GqKoreaAdapter(client=client).discover_article_urls(max_pages=3)

    assert client.calls, "discovery made no request at all"
    for _, body in client.calls:
        assert body["notInPosts"], f"notInPosts must be non-empty, got {body['notInPosts']!r}"


def test_driver_posts_the_documented_form_to_the_ajax_endpoint():
    # Every value below is a literal on purpose. Asserting against the module's
    # own constants (url == AJAX_ENDPOINT, posts_per_page == LISTING_PAGE_SIZE)
    # is a tautology that holds whatever those constants become — and what they
    # are is not an internal detail but the probed contract of a live endpoint.
    client = FakeClient([make_page(make_post("1"))])
    GqKoreaAdapter(client=client).discover_article_urls(max_pages=1)

    url, body = client.calls[0]
    assert url == "https://www.gqkorea.co.kr/wp-admin/admin-ajax.php"
    assert body["action"] == "get_posts_1depth_list"
    assert body["post_type"] == "post"
    assert body["tax1_slug"] == "style"
    assert body["posts_per_page"] == "50"
    assert body["paged"] == "1"
    # Post id 0 does not exist, so this excludes nothing real. The site's own JS
    # sends 17 "recommended" ids here, which would silently skip 17 articles.
    assert body["notInPosts"] == "0"


# --------------------------------------------------------------------------
# discover_article_urls — pagination
# --------------------------------------------------------------------------

def test_pagination_walks_pages_in_order_until_a_page_has_no_posts():
    client = FakeClient([
        make_page(make_post("1"), make_post("2")),
        make_page(make_post("3")),
    ])
    found = GqKoreaAdapter(client=client).discover_article_urls(max_pages=10)

    assert [e.post_id for e in found] == ["1", "2", "3"]
    # Page 3 comes back empty and stops the walk; nothing beyond it is fetched.
    assert client.paged_requested == ["1", "2", "3"]


def test_max_pages_caps_the_walk_short_of_the_end():
    # Five pages are on offer; the ceiling has to stop the walk at two, so that
    # a bug in the terminal condition costs one long run and not an endless one.
    client = FakeClient([make_page(make_post(str(n))) for n in range(1, 6)])
    found = GqKoreaAdapter(client=client).discover_article_urls(max_pages=2)

    assert client.paged_requested == ["1", "2"]
    assert [e.post_id for e in found] == ["1", "2"]


def test_max_pages_below_one_is_rejected_before_a_request_goes_out():
    client = FakeClient([make_page(make_post("1"))])
    with pytest.raises(ValueError, match="max_pages"):
        GqKoreaAdapter(client=client).discover_article_urls(max_pages=0)
    assert client.calls == []


# --------------------------------------------------------------------------
# discover_article_urls — the incremental stop rule
#
# `seen` holds permalinks whose articles carry a completion marker. A seen entry
# is skipped, never a stop: stopping at the first one strands everything behind
# a hole forever — a capped run, a run killed by the job timeout, an article
# whose images all failed. The walk stops on the first page that is entirely
# seen, which costs one extra POST per steady-state run.
# --------------------------------------------------------------------------

def test_a_hole_in_the_middle_of_the_stored_set_is_rediscovered():
    # The case the old first-seen-wins rule got wrong: post 2 is complete, post 3
    # is not (its images failed, or a --max-articles cap cut the run before it).
    # Returning at post 2 would leave post 3 unreachable by every future run.
    complete = make_post("2")
    hole = make_post("3")
    client = FakeClient([
        make_page(make_post("1"), complete, hole),
        make_page(make_post("4")),
    ])
    found = GqKoreaAdapter(client=client).discover_article_urls(
        max_pages=10, seen={complete["permalink"]}
    )

    assert [e.post_id for e in found] == ["1", "3", "4"]


def test_a_steady_state_run_stops_on_the_first_wholly_seen_page():
    # Two new posts at the top of page 1, everything else already complete.
    # Page 2 yields nothing unseen and ends the walk; page 3 is never fetched.
    old = [make_post(str(n)) for n in range(3, 9)]
    client = FakeClient([
        make_page(make_post("1"), make_post("2"), *old[:3]),
        make_page(*old[3:]),
        make_page(make_post("9")),
    ])
    found = GqKoreaAdapter(client=client).discover_article_urls(
        max_pages=10, seen={post["permalink"] for post in old}
    )

    assert [e.post_id for e in found] == ["1", "2"]
    assert client.paged_requested == ["1", "2"], "the walk should stop on the seen page"


def test_a_wholly_seen_first_page_stops_after_one_request():
    # Nothing published since the last run: one POST, no entries, no page 2.
    posts = [make_post("1"), make_post("2")]
    client = FakeClient([make_page(*posts), make_page(make_post("3"))])
    found = GqKoreaAdapter(client=client).discover_article_urls(
        max_pages=10, seen={post["permalink"] for post in posts}
    )

    assert found == []
    assert client.paged_requested == ["1"]


def test_max_pages_still_caps_a_cold_walk_with_nothing_seen():
    # The backfill path: every page is new, so only the ceiling stops it.
    client = FakeClient([make_page(make_post(str(n))) for n in range(1, 6)])
    found = GqKoreaAdapter(client=client).discover_article_urls(max_pages=3, seen=frozenset())

    assert client.paged_requested == ["1", "2", "3"]
    assert [e.post_id for e in found] == ["1", "2", "3"]


def test_a_page_of_only_skipped_rows_is_not_the_end_of_the_list():
    # Every row on page 1 is parent-only STYLE, so it yields no entries — but
    # the list plainly continues. Conflating "nothing usable here" with "no more
    # posts" would truncate a backfill at the first such page.
    client = FakeClient([
        make_page(make_post("1", category="STYLE")),
        make_page(make_post("2")),
    ])
    found = GqKoreaAdapter(client=client).discover_article_urls(max_pages=10)

    assert [e.post_id for e in found] == ["2"]


def test_a_permalink_repeated_across_pages_is_returned_once():
    # Offset pagination over a live list: an article published mid-crawl shifts
    # every later page by one, so the same post can land on two pages.
    duplicate = make_post("1")
    client = FakeClient([make_page(duplicate), make_page(duplicate, make_post("2"))])
    found = GqKoreaAdapter(client=client).discover_article_urls(max_pages=10)

    assert [e.post_id for e in found] == ["1", "2"]


def test_a_page_of_entries_we_already_collected_is_not_a_finished_page():
    # The distinction the stop rule turns on, and the one a dedupe-first ordering
    # quietly loses. "This page had no unseen entries" means everything from here
    # back is complete: stop. "This page had no entries we had not already
    # collected" means offset drift replayed rows we are already holding — the
    # very case the collected set exists for, since an article deleted mid-crawl
    # shifts every later page back and can repeat a page's worth of rows. Reading
    # the replay as a finished page truncates the walk at the drift and loses
    # everything behind it, with no error and no missing count.
    replayed = [make_post("1"), make_post("2")]
    client = FakeClient([
        make_page(*replayed),
        make_page(*replayed),  # the same rows again, shifted back onto page 2
        make_page(make_post("3")),
    ])
    found = GqKoreaAdapter(client=client).discover_article_urls(max_pages=10)

    assert [e.post_id for e in found] == ["1", "2", "3"]
    assert client.paged_requested == ["1", "2", "3", "4"], "the replay must not stop the walk"


def test_driver_raises_on_the_taxonomy_sentinel_instead_of_reporting_zero():
    # The sentinel response carries no current_posts either, so a driver that
    # tested for end-of-list before validating the term would swallow it and
    # return an empty list — the silent failure this whole check exists to stop.
    client = FakeClient([{"current_term": {"tax1_term": False}}])
    with pytest.raises(ValueError, match="renamed"):
        GqKoreaAdapter(client=client).discover_article_urls(max_pages=10)


def test_an_empty_first_page_raises_because_page_1_is_never_legitimately_empty():
    # STYLE holds ~7,450 posts, so nothing on page 1 is not the end of the list:
    # it is what the endpoint returns once it stops accepting our parameters —
    # a notInPosts, posts_per_page or paged change in a theme update. That
    # response is byte-identical to end-of-list, so unless page 1 is special-
    # cased, going blind reports articles_seen=0 and exits 0 forever.
    client = FakeClient([END_OF_LIST])
    with pytest.raises(ValueError, match="notInPosts"):
        GqKoreaAdapter(client=client).discover_article_urls(max_pages=10)


def test_a_later_empty_page_is_still_just_the_end_of_the_list():
    # The counterpart: past the last page the same response is entirely normal.
    client = FakeClient([make_page(make_post("1")), END_OF_LIST])
    found = GqKoreaAdapter(client=client).discover_article_urls(max_pages=10)

    assert [e.post_id for e in found] == ["1"]


# --------------------------------------------------------------------------
# discover_article_urls — client ownership
# --------------------------------------------------------------------------

def test_an_adapter_with_no_client_opens_exactly_one_and_closes_it(monkeypatch):
    # The pipeline injects a client so the rate limiter sees every request; a
    # bare adapter opens its own instead, and the `with` is what says we are the
    # ones who close it. A leaked connection pool per run is invisible in the
    # tests that inject a client, so this is the only place that can catch it.
    # Substituting the class here is also what keeps this test socket-free.
    from gallery_scraper.core import http  # imported at call time, as the adapter does

    opened = []

    class OwnedClient(FakeClient):
        """A PoliteClient stand-in that records how it was opened and closed."""

        def __init__(self, *_args, **_kwargs) -> None:
            super().__init__([make_page(make_post("1"))])
            self.closed = False
            opened.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            self.closed = True
            return False

    monkeypatch.setattr(http, "PoliteClient", OwnedClient)
    found = GqKoreaAdapter().discover_article_urls(max_pages=1)

    assert [e.post_id for e in found] == ["1"]
    assert len(opened) == 1, f"expected one client for the walk, got {len(opened)}"
    assert opened[0].closed, "the client discovery opened was never closed"
