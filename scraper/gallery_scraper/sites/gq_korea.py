"""GQ Korea Style-tab adapter.

Selectors here were derived from a real article captured 2026-08-23 and then
re-checked against six captures spanning all five categories and a 2024 page;
the shape is pinned by tests/fixtures/article_pictorial.html. When GQ restyles
the site, this module and that fixture are what change — nothing downstream.

That re-check is worth its own note. The first pass scoped body images to
div.post_content on the strength of PLAN.md's claim that the container "keeps
the author avatar and the recommendation modules out". It does not: both sit
inside it on every capture, and a live --dry-run re-hosted four pieces of
chrome per article while the suite stayed green, because the fixture had been
built to agree with the code rather than with the page. _images carries the
measured rule and the reasoning; the moral is in the fixture header.

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
# publication or a disclosure rather than a person. Letting these through would
# put a non-person into person_name and pollute the v1.1 credit-person filter.
#
# 출처 ("source") is here on evidence, not on principle: one capture prints
# <dt>출처</dt><dd><a href="…">domain</a></dd>, the syndication row naming the
# title an article was translated from. Its <dd> text is that site's hostname,
# so before this entry every syndicated article stored a domain as a person.
#
# The screen is on the LABEL and never on the value, and that limit is
# deliberate. Two captures print an organisation under 사진 — a stock-photo
# agency on one, a phrase naming the brands rather than any photographer on the
# other — so person_name still holds the occasional non-person after this
# filter, and 사진 is emphatically NOT a candidate for this set: it is the
# ordinary label for a real photographer on any shoot. No value-side test tells
# an agency from a person's name without a curated vocabulary of agency words,
# which fails open on every agency missing from it and, worse, fails closed on
# any person whose name contains one — silently dropping a real credit. A
# stored organisation is visible in the data and fixable downstream; a dropped
# photographer is invisible. So the value is stored as printed, and the v1.1
# credit-person filter must treat photographer values as person-or-organisation.
_NON_PERSON_ROLES = frozenset(
    {
        "sponsored by",
        "sponsor",
        "협찬",
        "제공",
        "in partnership with",
        "advertorial",
        "출처",
    }
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

# ---- body-image scoping, measured against six captures --------------------
#
# Widths of the net, narrowest last. See _images for what each one lets in.
_POST_CONTENT = "div.post_content"  # the whole article column, chrome included
_CONTENT_WELL = "div.contt"  # the WordPress content well: editorial only

# The one chrome container that lives *inside* the content well, plus the
# <noscript> twin that wraps a duplicate of nearly every lazy-loaded image.
# Both are matched anywhere below the well, not just as direct children.
_CHROME_INSIDE_WELL = "noscript, .relate_group"

_AT_SPLIT = re.compile(r"\s+at\s+", re.IGNORECASE)
# One credit row can name several people. Measured: the stylist row on one
# capture prints three comma-separated names. Only the ASCII comma occurs; the
# Korean typographic separators (、·) appear nowhere in the captured credits,
# so they are not guessed at here.
_COMMA_SPLIT = re.compile(r"\s*,\s*")
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


def split_credit_people(value: str) -> tuple[tuple[str, str | None], ...]:
    """One credit <dd> as (person, agency) pairs, in printed order.

    Ten of the eleven credit rows in the six captures name one person; the
    eleventh names THREE, as `이름, 이름, 이름 at 에이전시` — a stylist team
    written out on one line, with the agency after the last of them.
    Joining those into a single person_name — which is what happened before
    this function existed, because split_person_and_agency only ever cut on
    " at " — stored a string no credit-person filter can undo. article_credits
    is one row per person, replace_credits numbers them by position, and
    `unique (article_id, position)` lets several rows share a role, so one pair
    per name is the shape the schema was built for.

    The agency binds ONLY to the name it is written beside, so in the observed
    line the first two people come back with agency None. The competing reading
    — a trailing agency distributes over the whole list, since that is plausibly
    why the editor wrote it once instead of three times — is rejected on two
    grounds. First, this module's standing rule is that a null beats a guess:
    normalize_role returns None rather than guessing a role, and an affiliation
    the page never printed for those two people is the same kind of invention,
    except that it lands in a column a reader will believe. Second, per-part
    parsing needs no positional magic and so has no order to get wrong: it
    already handles `A at X, B at Y`, where a distribute-the-trailing-agency
    rule has to decide what a *leading* agency does. Reversing this decision is
    one line, and test_gq_korea.py names the test that pins it.
    """
    pairs = (split_person_and_agency(part) for part in _COMMA_SPLIT.split(value))
    # An empty part is a trailing or doubled comma. No capture prints one, but
    # a blank <dd> reaches here as a single empty part, and _credits relies on
    # this filter for that case rather than testing the value itself.
    return tuple((name, agency) for name, agency in pairs if name)


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

    That skip is half of an invariant the parser holds the other half of:
    GqKoreaAdapter._category *raises* on the same parent-only article, whose
    breadcrumb is [홈, STYLE, <title>] with no subcategory in it. The halves
    agree today — nothing that fails there can reach here — and they only stay
    agreeing while both are read together, so each names the other. Widen this
    filter to pass parent-only rows and every run starts dying on the page they
    lead to; relax that raise to a default and discovery still never delivers
    the articles the default was written for.
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

        The trail is [홈, STYLE, <subcategory>, <title>] on five of the six
        captures and [홈, STYLE, <title>] on the sixth — the LAST crumb is the
        article's own title, never its category. See below for why that decides
        the scan direction, and what the sixth shape means.
        """
        nav = tree.css_first('nav[aria-label="breadcrumb"]')
        if nav is None:
            raise ValueError("no breadcrumb found — page layout changed")

        crumbs = [_clean(li.text()) for li in nav.css("li")]

        # Document order, not reversed. Scanning backwards evaluates the title
        # crumb FIRST on every live page and reaches the real subcategory only
        # because a headline rarely happens to *be* a category name — but two
        # of the five are ordinary English words a GQ headline can consist of,
        # and the first article titled exactly "news" or "item" would be filed
        # by its headline instead of by its section, silently and forever.
        # Forwards, the two crumbs ahead of the subcategory are 홈 and STYLE,
        # and neither is a subcategory, so the first match is the one that
        # describes the article.
        for crumb in crumbs:
            candidate = crumb.lower()
            if candidate in CATEGORIES:
                return candidate

        # [홈, STYLE, <title>]: an article filed under the parent term only,
        # which one capture is. Raising is right — article_category is a
        # Postgres enum of the five children, so there is nothing to store and
        # any fallback would be a wrong guess about a real article.
        #
        # This is the article half of an invariant whose other half is in
        # _listing_entry: discovery drops exactly these rows, because their
        # post_terms is the parent "STYLE" and CATEGORIES holds only the
        # children. So the pipeline never fetches one and this raise is
        # unreachable in a normal run — it fires only for a hand-fed URL or
        # after someone widens one half without the other. The two are one edit
        # apart from disagreeing, which is why each names the other.
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
        """The by-line's linked author — deliberately the linked one, and only one.

        Measured on all six captures: span.author holds exactly ONE
        <a href="/author/…">, never two. A co-author is printed as bare text
        beside it — `<a href="/author/…">이름</a>, 다른 이름` on one of the six.
        So reading the span's text instead of the link would put ", 다른 이름"
        into author_name, and there is no second href to put anywhere.

        Keeping only the link loses nothing. articles.author_name and
        author_url are single nullable columns describing one person, PLAN.md
        puts further contributors in credits, and on the one capture that has a
        co-author div.info_area already credits that same name under 글
        (writer) — so the data is stored, with its role, in the place built for
        it. Splitting the span on commas would duplicate that credit into a
        column that cannot hold a URL for it. The fixture encodes the observed
        by-line and test_gq_korea.py pins both halves of this choice.
        """
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

        One <dl> is one printed row and NOT necessarily one credit: the <dt> is
        screened as a label (see _NON_PERSON_ROLES) and the <dd> can name
        several people (see split_credit_people), so a row yields zero, one or
        several Credits and the caller must not assume a row count.
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

            # One row, one *or more* people — see split_credit_people. It cleans
            # its own input and answers () for a blank one, so an empty <dd> and
            # a whitespace-only one both fall out here with no separate check.
            role = normalize_role(role_raw)
            for person_name, agency in split_credit_people(dd_node.text()):
                credits.append(
                    Credit(
                        role_raw=role_raw,
                        person_name=person_name,
                        role=role,
                        agency=agency,
                    )
                )
        return tuple(credits)

    def _images(self, tree: HTMLParser) -> tuple[ImageRef, ...]:
        """Body images only, in source order, deduplicated.

        Scope is div.post_content > … > div.contt, minus two things nested
        inside it. PLAN.md said div.post_content alone "keeps the author avatar
        and the recommendation modules out"; six captures say it does not, and
        a live --dry-run re-hosted four pieces of chrome per article before
        anyone checked. What div.post_content actually contains, alongside the
        editorial well:

        * div.profile_sub — the author profile, a *direct child* of
          div.post_content and a sibling of div.editor. It holds a 600x600
          avatar (a shared theme placeholder on advertorials, so the same file
          over and over).
        * div.relate_group.relate_content — the "MORE LIKE THIS" module, three
          500x500 thumbnails belonging to three *other* articles. It is nested
          inside div.contt, so neither div.editor nor div.contt excludes it on
          its own and it has to be named.
        * div.news_group, div.banner_area, div.m-hotpick, div.info_area,
          div.ad_wrapper, div.sponsored-txt, ul.share_list — recommendation
          surfaces, ad slots and share bars. All image-free in the raw HTML
          today because they hydrate client-side, which is exactly why a rule
          scoped above div.contt looks correct and rots the day one of them
          prints a thumbnail server-side.

        Over-collecting here is not cosmetic: every extra <img> is downloaded,
        re-hosted and stored as *this* article's gallery, so a stranger's
        thumbnail and a duplicated avatar end up in the grid and eat the 1 GB
        free tier. On the sneakers capture the old rule returned more chrome
        than content — four wrong out of seven.

        The well is an allowlist and the two exclusions inside it a denylist,
        deliberately in that order: an editorial block type GQ enables tomorrow
        appears inside div.contt and is collected, where an allowlist of known
        blocks would silently drop it. That is not hypothetical. Four unrelated
        editorial shapes are already in the captures, and between them they
        account for all 44 editorial images with no remainder:

        * figure.wp-block-image — 24 images, on five of the six captures
        * ul.item_list.shopping_list > li.full.shopping_item > div.thum —
          10 images, all on the "item" capture, which has no figure at all
        * div.gallery_wrap > … > div.swiper-slide (Swiper) — 8 images
        * div.content_columns.two > div.content_column — 2 images

        So a figure-scoped rule loses a whole article; one narrowed to figures,
        columns and direct children of div.contt loses 18 of the 44. Adding
        `ul` or `li` to the denylist erases the "item" capture outright. And
        div.thum, tempting as a chrome marker because the relate_group uses it,
        is the wrapper those same 10 editorial images sit in — blocklisting it
        costs the article its entire gallery. All four shapes are in the
        fixture, and tests assert each one still reaches the output.

        <noscript> is excluded even though nothing needs it to be today: it
        wraps a byte-identical twin of nearly every lazy image, and the URL
        dedupe below currently collapses those twins by accident of document
        order. That is load-bearing work the dedupe was not written to do, and
        a twin carrying a different crop would append a phantom image.

        Dedup matters downstream: the same URL twice in one body would become
        two rows sharing (article_id, content_hash), and a single INSERT
        carrying both fails with cardinality_violation.

        A missing container raises, like every other required field here: both
        are one renamed class away in a theme update, and swallowing either
        would give articles that parse cleanly with zero images. An empty well
        still returns () — that is a content fact about a text-only post, not a
        structural surprise, and keeping the two apart is the whole point.
        """
        # Both messages name their own selector, and no message is a prefix or
        # a substring of the other. They used to share the words "post body",
        # so one `match="post body"` test accepted either — and deleting this
        # first raise in favour of `... or tree.root`, the exact widening the
        # test was written to forbid, left the whole suite green.
        body = tree.css_first(_POST_CONTENT)
        if body is None:
            raise ValueError(f"no {_POST_CONTENT} post body found — page layout changed")

        # Scoped to `body`, not to the document: resolving the well page-wide
        # would let a div.contt appearing earlier in the page — a sidebar, a
        # module the theme adds tomorrow — win over the article's own.
        well = body.css_first(_CONTENT_WELL)
        if well is None:
            raise ValueError(
                f"post body has no {_CONTENT_WELL} content well — page layout changed"
            )

        # A set, and that is load-bearing rather than a style choice. Measured
        # on selectolax 0.4.11: `==` between two nodes is STRUCTURAL — two
        # distinct <img> elements with identical markup compare equal — while
        # `hash()` is identity-based, so those same two hash differently. A set
        # buckets by hash before it ever calls `==`, so `img in chrome` answers
        # "is this the very node I collected?"; the same test against a list
        # would fall back to `==` alone and drop any editorial image whose
        # markup happens to match a recommendation thumbnail. Resolving once
        # also beats walking each image's ancestors.
        chrome = {
            img for wrapper in well.css(_CHROME_INSIDE_WELL) for img in wrapper.css("img")
        }

        seen: set[str] = set()
        images: list[ImageRef] = []
        for img in well.css("img"):
            if img in chrome:
                continue
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

        We take the rendition the page itself serves and never the widest one
        offered, and that is a decision rather than an oversight — the srcset
        does routinely advertise something bigger. Measured over the 44
        editorial images in the six captures: data-src is always WordPress's
        largest *derivative* — its long edge is 1400px on 31 of them and 930px
        on 11, and nothing in a srcset ever beats it except the unresized
        original, which carries no -WxH suffix and is sized only by its width
        descriptor (1205w … 1920w), and four wider derivatives on two of the
        captures. 24 of the 44 have such a candidate, and taking the best one
        at or above images.MAX_EDGE would lift all 24 off their 1400px long
        edge — 21 to a full 1600px, three to 1462px.

        Against that: +14% on the long edge is ~+31% more pixels stored for
        each of those 21 and ~+18% across the corpus, charged against a 1 GB
        free tier that is the binding constraint on how long this gallery can
        keep running — and the download is a full-resolution master whose byte
        size the markup does not advertise anywhere, so the fetch cost cannot
        be measured from a capture at all, only discovered in production
        against a site we are trying to be light on. A 1400px source already
        exceeds what the grid renders. So the cheap, page-sanctioned rendition
        wins, and the srcset stays what it is below: a last-resort fallback for
        the images that carry no data-src, where its FIRST candidate is the
        same largest derivative on every capture that has one.
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
