"""GQ Korea Style-tab adapter.

Selectors here were derived from a real article captured 2026-08-23; the shape
is pinned by tests/fixtures/article_pictorial.html. When GQ restyles the site,
this module and that fixture are what change — nothing downstream.

Two findings from that capture worth recording, because they contradict
PLAN.md's original assumptions:

1. **No browser needed for articles.** PLAN.md assumed Playwright because body
   images are lazy-loaded. They are — `src` holds a base64 placeholder — but the
   real URL is served in `data-src` in the raw HTML, so a plain HTTP fetch is
   enough. That drops a heavy dependency from the per-article path and is
   considerably politer: one request instead of a headless browser session.
   Discovery is the part that still needs rendering; the category grid is
   hydrated client-side.

2. **The WordPress REST API is closed** (`/wp-json/` -> 401), so the structured
   shortcut is unavailable and HTML parsing stands.
"""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from gallery_scraper.core.adapter import ArticleData, Credit, ImageRef

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

_AT_SPLIT = re.compile(r"\s+at\s+", re.IGNORECASE)
_WS = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip()


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


class GqKoreaAdapter:
    site = "gq_korea"

    # ---- discovery -------------------------------------------------------

    def discover_article_urls(self, category: str) -> list[str]:
        raise NotImplementedError(
            "Listing grids are hydrated client-side; discovery needs a rendered "
            "page or the admin-ajax endpoint robots.txt explicitly allows."
        )

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
            if dt_node is None or dd_node is None:
                continue

            role_raw = _clean(dt_node.text())
            value = _clean(dd_node.text())
            if not role_raw or not value:
                continue
            if role_raw.lower() in _NON_PERSON_ROLES:
                continue

            person_name, agency = split_person_and_agency(value)
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
        """
        body = tree.css_first("div.post_content")
        if body is None:
            return ()

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
