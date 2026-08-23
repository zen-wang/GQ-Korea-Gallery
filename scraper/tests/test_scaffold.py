"""Phase 0 smoke: the package skeleton imports cleanly without heavy deps installed."""

import importlib

import pytest

MODULES = [
    "gallery_scraper",
    "gallery_scraper.core.adapter",
    "gallery_scraper.sites.gq_korea",
    "gallery_scraper.images",
    "gallery_scraper.storage",
    "gallery_scraper.db",
    "gallery_scraper.pipeline",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    importlib.import_module(name)
