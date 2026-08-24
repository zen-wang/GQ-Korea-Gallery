"""Site-agnostic scraping contracts. Concrete adapters live in gallery_scraper.sites."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date
from typing import Protocol

# Crawl-depth ceiling every adapter honours, so a bug in a listing's terminal
# condition costs one long run rather than an unbounded one.
DEFAULT_MAX_PAGES = 200


@dataclass(frozen=True)
class Credit:
    role_raw: str  # as printed, e.g. '포토그래퍼'
    person_name: str
    role: str | None = None  # normalized, e.g. 'photographer'
    agency: str | None = None  # the '... at 에이전시' suffix, when present


@dataclass(frozen=True)
class ImageRef:
    source_url: str
    position: int  # 1-based order within the article body


@dataclass(frozen=True)
class ListingEntry:
    """One row of a listing page: enough to decide whether to fetch the article.

    The article page stays authoritative for everything it also carries — title
    and date are kept here for logging and for ordering decisions during a
    crawl, not as a substitute for parsing the page.
    """

    source_url: str
    category: str  # already validated against the adapter's known categories
    post_id: str  # the CMS id, as sent: a string, not an int
    title: str
    published_date: date | None


@dataclass(frozen=True)
class ArticleData:
    source_url: str
    category: str
    title: str
    published_date: date | None = None
    author_name: str | None = None
    author_url: str | None = None
    credits: tuple[Credit, ...] = ()
    images: tuple[ImageRef, ...] = ()


class SiteAdapter(Protocol):
    site: str

    def discover_article_urls(
        self,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        seen: AbstractSet[str] = frozenset(),
    ) -> list[ListingEntry]: ...

    def parse_article(self, html: str, source_url: str) -> ArticleData: ...
