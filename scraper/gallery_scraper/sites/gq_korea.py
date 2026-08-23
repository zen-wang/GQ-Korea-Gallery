"""GQ Korea Style-tab adapter. Parsers are built test-first in Phase 3."""

from __future__ import annotations

from gallery_scraper.core.adapter import ArticleData

BASE_URL = "https://www.gqkorea.co.kr"
CATEGORIES = ("grooming", "item", "news", "pictorial", "sneakers")


class GqKoreaAdapter:
    site = "gq_korea"

    def discover_article_urls(self, category: str) -> list[str]:
        raise NotImplementedError("Phase 3: listing crawl with stop-at-seen incremental logic")

    def parse_article(self, html: str, source_url: str) -> ArticleData:
        raise NotImplementedError("Phase 3: breadcrumb category, header, body images, credits")
