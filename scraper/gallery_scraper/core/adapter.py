"""Site-agnostic scraping contracts. Concrete adapters live in gallery_scraper.sites."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


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

    def discover_article_urls(self, category: str) -> list[str]: ...

    def parse_article(self, html: str, source_url: str) -> ArticleData: ...
