"""GQ Korea Style-tab adapter.

Selectors here were derived from a real article captured 2026-08-23; the shape
is pinned by tests/fixtures/article_pictorial.html. When GQ restyles the site,
this module and that fixture are what change — nothing downstream.

Three findings worth recording, because each contradicts an assumption PLAN.md
made before anyone had looked at the markup:

1. **No browser needed for articles.** PLAN.md assumed Playwright because body
   images are lazy-loaded. They are — `src` holds a base64 placeholder — but the
   real URL is served in `data-src` in the raw HTML, so a plain HTTP fetch is
   enough. That drops a heavy dependency from the per-article path and is
   considerably politer: one request instead of a headless browser session.

2. **The WordPress REST API is closed** (`/wp-json/` -> 401), so the structured
   shortcut is unavailable and HTML parsing stands.

3. **No browser needed for discovery either.** The category grid is hydrated
   client-side, but the `admin-ajax.php` endpoint behind it is the one
   `/wp-admin/` path robots.txt explicitly re-allows, and it answers a plain
   form POST. Its quirks are documented on the listing constants below and
   pinned by tests/test_discovery.py. Playwright is needed nowhere here.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from gallery_scraper.core.adapter import (
    DEFAULT_MAX_PAGES,
    ArticleData,
    Credit,
    ImageRef,
    ListingEntry,
)

if TYPE_CHECKING:  # a type name only — see GqKoreaAdapter.discover_article_urls
    from gallery_scraper.core.http import PoliteClient

BASE_URL = "https://www.gqkorea.co.kr"
CATEGORIES = ("grooming", "item", "news", "pictorial", "sneakers")

# GQ Korea publishes from Seoul and its <meta> timestamps are UTC. A post at
# 22:00 KST is stamped 13:00Z the same day, but one at 00:30 KST is stamped
# 15:30Z the *previous* day — so taking the UTC date directly would file some
# articles a day early. The date a reader sees is the KST one.
KST = dt.timezone(dt.timedelta(hours=9))

# Korean role label -> normalized slug. Exact match only: guessing by substring
# would fold "패션 에디터" into "에디터" and lose the distinction. An unknown
# label yields None and keeps role_raw, which is why role is nullable in the
# schema — a new role should surface as unnormalized, never as a wrong guess.
_ROLES: dict[str, str] = {
    "포토그래퍼": "photographer",
    "사진": "photographer",
    "포토": "photographer",
    "모델": "model",
    "스타일리스트": "stylist",
    "스타일링": "stylist",
    "헤어": "hair",
    "메이크업": "makeup",
    "메이크 업": "makeup",
    "에디터": "editor",
    "패션 에디터": "fashion_editor",
    "피처 에디터": "feature_editor",
    "아트 디렉터": "art_director",
    "프로덕션": "production",
    "어시스턴트": "assistant",
    "글": "writer",
    "번역": "translator",
}

# Rows that live in the same <dl> list as the credits but name a brand, a
# campaign or a disclosure rather than a person. Letting these through would
# put a brand into person_name and pollute the v1.1 credit-person filter.
_NON_PERSON_ROLES = frozenset(
    {"sponsored by", "sponsor", "협찬", "제공", "in partnership with", "advertorial"}
)

# ---- listing endpoint, probed live 2026-08-23 (see README.md) --------------

# robots.txt disallows /wp-admin/ and then explicitly re-allows this one path.
AJAX_ENDPOINT = f"{BASE_URL}/wp-admin/admin-ajax.php"
LISTING_ACTION = "get_posts_1depth_list"
LISTING_TAXONOMY = "style"
LISTING_PAGE_SIZE = 50  # verified working; the site's own grid asks for 12
FIRST_PAGE = 1  # `paged` is 1-based, and page 1 gets treated differently below

# An exclusion list that MUST be present and non-empty, or the response omits
# current_posts entirely and discovery goes quietly blind. Post id 0 does not
# exist, so nothing real is excluded. The site's own JS hardcodes 17
# "recommended" ids here; copying that would silently skip those 17 articles.
LISTING_NOT_IN_POSTS = "0"

# A response carrying only `current_term` is how the end of the list reads — and
# it is byte-identical to what comes back when the endpoint stops accepting our
# parameters. On page 1 the two are told apart by arithmetic rather than by the
# response: the STYLE taxonomy holds ~7,450 posts, so an empty first page is
# never the end of anything. Left silent, discovery would report 0 new articles
# and exit 0 forever, which is the same failure the tax1_term sentinel exists to
# prevent — so it fails the same way, loudly and naming the likely causes.
_BLIND_DISCOVERY = (
    "admin-ajax returned no posts for page 1 of the Style listing, which holds "
    "thousands of them: either notInPosts stopped being accepted (an absent or "
    "empty one drops current_posts entirely) or posts_per_page/paged handling "
    "changed in a theme update. Discovery is blind until it is fixed."
)

# The listing prints the KST display date — unlike the article page's <meta>
# timestamp, there is no timezone conversion to do here.
_LISTING_DATE_FORMAT = "%Y.%m.%d"

_AT_SPLIT = re.compile(r"\s+at\s+", re.IGNORECASE)
_WS = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip()


def _text(value: object) -> str:
    """Clean text out of one JSON field, whatever the endpoint actually sent.

    Nothing in that response is schema-checked on the far side: post_id arrives
    as a string today and an int tomorrow would be nobody's bug but ours.
    """
    if value is None:
        return ""
    return _clean(value if isinstance(value, str) else str(value))


def normalize_role(role_raw: str) -> str | None:
    """Map a printed Korean role label to a stable slug, or None if unknown."""
    return _ROLES.get(_clean(role_raw))


def split_person_and_agency(value: str) -> tuple[str, str | None]:
    """Split the `이름 at 에이전시` form into its parts.

    Only a standalone "at" separates them, so a name that merely contains the
    letters (Nathan, Atkinson) stays intact.
    """
    cleaned = _clean(value)
    if not cleaned:
        return "", None
    parts = _AT_SPLIT.split(cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].strip(), parts[1].strip()
    return cleaned, None


def parse_listing_page(payload: object) -> tuple[ListingEntry, ...]:
    """Entries from one already-decoded admin-ajax response, in source order.

    Pure on purpose: every quirk of this endpoint is a quirk of the response
    shape, so all of them can be pinned by a fixture with no socket in sight.
    """
    entries = (_listing_entry(post) for post in _listing_posts(payload))
    return tuple(entry for entry in entries if entry is not None)


def _listing_posts(payload: object) -> tuple[Any, ...]:
    """Raw post objects from one response, once the taxonomy checks out.

    The end of the list reads as the `current_posts` key being *absent* rather
    than as an empty array — the site's own JS does `current_posts.length`
    unguarded and would throw there — so absent and empty fold into the same
    terminal answer.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(f"listing response is not a JSON object: {type(payload).__name__}")

    # `tax1_term: false` is what comes back for a slug the site does not know.
    # Swallowing it would render a renamed taxonomy as "0 new articles" on every
    # run forever, which is precisely the silent failure PLAN.md §Robustness
    # warns about — so it is loud, and it names the likely cause.
    term = payload.get("current_term")
    if isinstance(term, Mapping) and term.get("tax1_term", True) is False:
        raise ValueError(
            "admin-ajax returned current_term.tax1_term=false for "
            f"tax1_slug={LISTING_TAXONOMY!r}: the Style taxonomy has most likely "
            "been renamed. Discovery cannot see a single article until it is fixed."
        )

    posts = payload.get("current_posts")
    if posts is None:
        return ()
    if not isinstance(posts, list):
        raise ValueError(f"current_posts is not a list: {type(posts).__name__}")
    return tuple(posts)


def _listing_entry(post: object) -> ListingEntry | None:
    """One post object as a ListingEntry, or None when it is not ours to store.

    Skipping beats raising here. `post_terms` carries the parent "STYLE" for an
    article filed under no subcategory, and a subcategory GQ adds tomorrow would
    otherwise take down the whole page; either value would fail the insert
    anyway, since article_category is a Postgres enum of the five children.
    """
    if not isinstance(post, Mapping):
        return None

    category = _text(post.get("post_terms")).lower()
    if category not in CATEGORIES:
        return None

    source_url = _text(post.get("permalink"))
    if not source_url:
        return None  # the permalink is the identity; there is nothing to fetch

    return ListingEntry(
        source_url=source_url,
        category=category,
        post_id=_text(post.get("post_id")),
        title=_text(post.get("post_title")),
        published_date=_listing_date(post.get("post_date")),
    )


def _listing_date(value: object) -> dt.date | None:
    """Parse the listing's `YYYY.MM.DD`, or None when it is not that.

    A garbled date must not cost us the article: the article page carries an
    authoritative timestamp and published_date is nullable in the schema, so
    None here is a shrug, not a loss.
    """
    raw = _text(value)
    if not raw:
        return None
    try:
        return dt.datetime.strptime(raw, _LISTING_DATE_FORMAT).date()
    except ValueError:
        return None


def _listing_form(paged: int) -> dict[str, str]:
    """The form body for one listing page. All values are strings by contract."""
    if paged < FIRST_PAGE:
        raise ValueError(f"paged is 1-based, got {paged}")
    return {
        "action": LISTING_ACTION,
        "post_type": "post",
        "tax1_slug": LISTING_TAXONOMY,
        "posts_per_page": str(LISTING_PAGE_SIZE),
        "paged": str(paged),
        "notInPosts": LISTING_NOT_IN_POSTS,  # required and non-empty — see the constant
    }


class GqKoreaAdapter:
    site = "gq_korea"

    def __init__(self, client: PoliteClient | None = None) -> None:
        # Injected by tests and by the pipeline, which owns one client for the
        # whole run so the rate limiter sees every request. Left None, discovery
        # opens and closes its own; parse-only callers open no socket at all.
        self._client = client

    # ---- discovery -------------------------------------------------------

    def discover_article_urls(
        self,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        seen: AbstractSet[str] = frozenset(),
    ) -> list[ListingEntry]:
        """Walk the Style listing newest-first and return what is worth fetching.

        There is no per-category crawl any more, and no `category` argument: one
        endpoint returns all five subcategories in a single reverse-chronological
        stream, so asking once per category would be five times the requests for
        the same rows.

        `seen` holds the permalinks of articles already stored *and complete* —
        `articles.content_hash is not null`, the pipeline's completion marker.
        Entries in it are skipped; the walk stops on the first page that is
        entirely seen. See _crawl for why it is not the first seen entry.
        """
        if max_pages < 1:
            raise ValueError(f"max_pages must be at least 1, got {max_pages}")

        if self._client is not None:
            return self._crawl(self._client, max_pages=max_pages, seen=seen)

        # Imported at call time, not module scope: this module stays importable
        # without the HTTP stack, and the `with` makes the ownership plain — we
        # opened this client, so we are the ones who close it.
        from gallery_scraper.core.http import PoliteClient

        with PoliteClient() as client:
            return self._crawl(client, max_pages=max_pages, seen=seen)

    def _crawl(
        self,
        client: PoliteClient,
        *,
        max_pages: int,
        seen: AbstractSet[str],
    ) -> list[ListingEntry]:
        """Walk pages until one holds nothing we have not already finished.

        A seen entry is *skipped*, not a stop: returning at the first one strands
        work permanently. A `--max-articles 20` run over 100 discovered articles
        stores the newest 20, and the next run would then hit article #1 on page
        1 and return nothing — articles 21-100 unreachable by every future run.
        The same trap catches a run killed by the job timeout, and an article
        whose images all failed and which therefore has no completion marker yet.

        Stopping on the first *fully* seen page costs one extra listing POST in
        steady state (page 1 has the new items, page 2 is all seen) and buys
        that every such hole heals on the next run instead of persisting.
        """
        found: list[ListingEntry] = []
        collected: set[str] = set()

        for paged in range(FIRST_PAGE, max_pages + 1):
            payload = client.post_json(AJAX_ENDPOINT, _listing_form(paged))

            # End-of-list is judged on the raw posts, not on the parsed entries:
            # a page of nothing but parent-only "STYLE" rows filters down to
            # nothing while the list plainly continues. _listing_posts also
            # validates the taxonomy first, so the sentinel raises here instead
            # of passing for the end of the list — that response has no
            # current_posts either.
            if not _listing_posts(payload):
                # Page 1 is the one page that is never legitimately empty.
                if paged == FIRST_PAGE:
                    raise ValueError(_BLIND_DISCOVERY)
                break

            entries = parse_listing_page(payload)
            unseen = 0
            for entry in entries:
                if entry.source_url in seen:
                    continue
                unseen += 1
                # Offset pagination over a live list: an article published
                # mid-crawl shifts every later page by one, so the same
                # permalink can land on two pages.
                if entry.source_url in collected:
                    continue
                collected.add(entry.source_url)
                found.append(entry)

            # Only a page that produced entries can end the walk. A page whose
            # rows were all filtered out (parent-only "STYLE", a subcategory GQ
            # added yesterday) yields nothing while the list plainly continues,
            # and must not be read as "everything from here back is complete".
            if entries and not unseen:
                break

        return found

    # ---- article ---------------------------------------------------------

    def parse_article(self, html: str, source_url: str) -> ArticleData:
        tree = HTMLParser(html)
        author_name, author_url = self._author(tree)
        return ArticleData(
            source_url=source_url,
            category=self._category(tree),
            title=self._title(tree),
            published_date=self._published_date(tree),
            author_name=author_name,
            author_url=author_url,
            credits=self._credits(tree),
            images=self._images(tree),
        )

    # ---- field parsers ---------------------------------------------------

    def _category(self, tree: HTMLParser) -> str:
        """Read the category from the breadcrumb: 홈 > STYLE > pictorial.

        PLAN.md picks the breadcrumb deliberately: an article page also carries
        "MORE LIKE THIS" and "MUST READ" modules advertising other categories,
        and the breadcrumb is the only element describing *this* article.
        """
        nav = tree.css_first('nav[aria-label="breadcrumb"]')
        if nav is None:
            raise ValueError("no breadcrumb found — page layout changed")

        crumbs = [_clean(li.text()) for li in nav.css("li")]
        for crumb in reversed(crumbs):
            candidate = crumb.lower()
            if candidate in CATEGORIES:
                return candidate

        raise ValueError(f"breadcrumb has no known Style category: {crumbs}")

    def _title(self, tree: HTMLParser) -> str:
        h1 = tree.css_first("h1.post_tit")
        if h1 is not None and _clean(h1.text()):
            return _clean(h1.text())
        og = tree.css_first('meta[property="og:title"]')
        if og is not None and _clean(og.attributes.get("content")):
            return _clean(og.attributes.get("content"))
        raise ValueError("no title found — page layout changed")

    def _published_date(self, tree: HTMLParser) -> dt.date | None:
        meta = tree.css_first('meta[property="article:published_time"]')
        if meta is None:
            return None
        raw = _clean(meta.attributes.get("content"))
        if not raw:
            return None
        try:
            stamp = dt.datetime.fromisoformat(raw)
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return stamp.astimezone(KST).date()

    def _author(self, tree: HTMLParser) -> tuple[str | None, str | None]:
        """First byline. Articles can credit several authors; the rest appear in
        the credits block, so only the primary one goes on the article row."""
        for link in tree.css('a[href*="/author/"]'):
            href = link.attributes.get("href") or ""
            name = _clean(link.text())
            if not name:
                # The avatar link wraps an <img> and carries no text of its own.
                img = link.css_first("img")
                if img is not None:
                    name = _clean(img.attributes.get("alt"))
            if name:
                return name, urljoin(BASE_URL, href)
        return None, None

    def _credits(self, tree: HTMLParser) -> tuple[Credit, ...]:
        """Parse div.info_area > dl > dt/dd into ordered credits.

        ul.tag_list sits inside the same container; scoping to <dl> keeps post
        tags out without needing to know what any given tag says.
        """
        info = tree.css_first("div.info_area")
        if info is None:
            return ()

        credits: list[Credit] = []
        for dl in info.css("dl"):
            dt_node = dl.css_first("dt")
            dd_node = dl.css_first("dd")
            # A half-filled row is what the CMS prints when a credit field is
            # left blank. Every skip below is one of those: markup, not a credit.
            if dt_node is None or dd_node is None:
                continue

            role_raw = _clean(dt_node.text())
            if not role_raw:
                continue
            if role_raw.lower() in _NON_PERSON_ROLES:
                continue

            # split_person_and_agency cleans its own input and answers "" for a
            # blank one, so this single check covers both an empty <dd> and a
            # whitespace-only one — no separate emptiness test is needed above.
            person_name, agency = split_person_and_agency(dd_node.text())
            if not person_name:
                continue

            credits.append(
                Credit(
                    role_raw=role_raw,
                    person_name=person_name,
                    role=normalize_role(role_raw),
                    agency=agency,
                )
            )
        return tuple(credits)

    def _images(self, tree: HTMLParser) -> tuple[ImageRef, ...]:
        """Body images only, in source order, deduplicated.

        Scoping to div.post_content is what keeps the author avatar and the
        recommendation modules out — PLAN.md names that as the main hazard.
        Dedup matters downstream: the same URL twice in one body would become
        two rows sharing (article_id, content_hash), and a single INSERT
        carrying both fails with cardinality_violation.

        A missing container raises, like every other required field here: it is
        one renamed class away in a theme update, and swallowing it would give
        articles that parse cleanly with zero images. An empty container still
        returns () — that is a content fact about a text-only post, not a
        structural surprise, and keeping the two apart is the whole point.
        """
        body = tree.css_first("div.post_content")
        if body is None:
            raise ValueError("no post body found — page layout changed")

        seen: set[str] = set()
        images: list[ImageRef] = []
        for img in body.css("img"):
            url = self._image_url(img)
            if url is None or url in seen:
                continue
            seen.add(url)
            images.append(ImageRef(source_url=url, position=len(images) + 1))
        return tuple(images)

    @staticmethod
    def _image_url(img) -> str | None:
        """Real URL for one <img>, preferring the lazy-load attribute.

        `src` holds a base64 placeholder while the page is unhydrated, so
        data-src wins where present; some images are not lazy-loaded at all and
        carry the real URL in src directly.
        """
        for attr in ("data-src", "src"):
            value = _clean(img.attributes.get(attr))
            if value and not value.startswith("data:"):
                return value

        srcset = _clean(img.attributes.get("data-srcset") or img.attributes.get("srcset"))
        if srcset:
            first = srcset.split(",")[0].strip().split(" ")[0]
            if first and not first.startswith("data:"):
                return first
        return None
