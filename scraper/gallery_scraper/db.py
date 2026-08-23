"""Supabase upserts (articles, article_credits, images) via supabase-py. Phase 3.

supabase is imported lazily inside functions so the package imports without it.
"""

from __future__ import annotations

from gallery_scraper.core.adapter import ArticleData


def upsert_article(data: ArticleData) -> None:
    raise NotImplementedError("Phase 3: idempotent upsert keyed on source_url + content_hash")
