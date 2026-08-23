"""Parser tests for the GQ Korea adapter.

PLAN.md §Testing Strategy calls these the highest-value tests in the project:
the parsers are the part that silently breaks when the site is restyled, and
everything downstream trusts what they return.

They run against tests/fixtures/article_pictorial.html, whose DOM mirrors a real
article captured 2026-08-23 with synthetic text. See the comment at the top of
that file for the edge cases it encodes.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from gallery_scraper.sites.gq_korea import (
    GqKoreaAdapter,
    normalize_role,
    split_person_and_agency,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SOURCE_URL = "https://www.gqkorea.co.kr/2026/07/12/summer-black/"


@pytest.fixture(scope="module")
def article():
    html = (FIXTURES / "article_pictorial.html").read_text(encoding="utf-8")
    return GqKoreaAdapter().parse_article(html, SOURCE_URL)


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------

def test_category_comes_from_the_breadcrumb(article):
    # PLAN.md: the breadcrumb is authoritative. Recommendation modules elsewhere
    # on the page advertise other categories and must not win.
    assert article.category == "pictorial"


def test_title(article):
    assert article.title == "여름의 끝에서, 검정"


def test_published_date_is_a_date_not_a_string(article):
    assert article.published_date == dt.date(2026, 7, 12)


def test_primary_author(article):
    # Two authors are present; the byline takes the first.
    assert article.author_name == "김에디터"
    assert article.author_url == "https://www.gqkorea.co.kr/author/김에디터/"


def test_source_url_is_preserved(article):
    assert article.source_url == SOURCE_URL


# --------------------------------------------------------------------------
# Credits
# --------------------------------------------------------------------------

def test_credits_are_in_source_order(article):
    assert [c.role_raw for c in article.credits] == [
        "포토그래퍼",
        "패션 에디터",
        "모델",
        "헤어",
        "메이크업",
    ]


def test_credit_without_agency(article):
    photographer = article.credits[0]
    assert photographer.person_name == "장기평"
    assert photographer.agency is None


def test_credit_with_agency_is_split(article):
    model = next(c for c in article.credits if c.role_raw == "모델")
    assert model.person_name == "홍태준"
    assert model.agency == "에스팀"


def test_roles_are_normalized(article):
    by_raw = {c.role_raw: c.role for c in article.credits}
    assert by_raw["포토그래퍼"] == "photographer"
    assert by_raw["모델"] == "model"
    assert by_raw["헤어"] == "hair"
    assert by_raw["메이크업"] == "makeup"
    assert by_raw["패션 에디터"] == "fashion_editor"


def test_sponsor_row_is_not_a_person_credit(article):
    # "SPONSORED BY" sits in the same <dl> list as the real credits but names a
    # brand, not a person. Storing it as a person_name would pollute the
    # credit-person filter in v1.1.
    assert all(c.role_raw != "SPONSORED BY" for c in article.credits)


def test_tags_are_not_mistaken_for_credits(article):
    # ul.tag_list is a sibling of the credit <dl>s inside div.info_area.
    names = {c.person_name for c in article.credits}
    assert "화보" not in names and "블랙" not in names


# --------------------------------------------------------------------------
# Body images — the scoping hazard PLAN.md flags
# --------------------------------------------------------------------------

def test_only_body_images_are_collected(article):
    urls = [i.source_url for i in article.images]
    assert len(urls) == 3, urls


def test_lazy_loaded_url_comes_from_data_src(article):
    urls = [i.source_url for i in article.images]
    assert "https://img.gqkorea.co.kr/gq/2026/07/style_aaaaaaaaaaaa1-1400x933.jpg" in urls
    assert not any(u.startswith("data:") for u in urls)


def test_non_lazy_image_is_read_from_src(article):
    urls = [i.source_url for i in article.images]
    assert "https://img.gqkorea.co.kr/gq/2026/07/style_aaaaaaaaaaaa2-1050x1400.jpg" in urls


def test_chrome_and_recommendations_are_excluded(article):
    urls = " ".join(i.source_url for i in article.images)
    assert "facebook.com" not in urls          # tracking pixel
    assert "logo.svg" not in urls              # site chrome
    assert "bbbbbbbbbbbb1" not in urls         # author avatar
    assert "cccccccccccc1" not in urls         # MUST READ module


def test_repeated_image_is_collected_once(article):
    # The same URL appears twice in the body. A single INSERT carrying both
    # would fail on unique (article_id, content_hash) with cardinality_violation
    # — supabase/README.md records this as a pipeline requirement.
    urls = [i.source_url for i in article.images]
    assert len(urls) == len(set(urls))


def test_image_positions_are_1_based_and_contiguous(article):
    assert [i.position for i in article.images] == [1, 2, 3]


# --------------------------------------------------------------------------
# Helpers, unit level
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("홍태준 at 에스팀", ("홍태준", "에스팀")),
        ("장기평", ("장기평", None)),
        ("  한지선  at  비트앤부트  ", ("한지선", "비트앤부트")),
        ("이신애 AT 멥시", ("이신애", "멥시")),
        # "at" inside a name must not split it
        ("Nathan Kim", ("Nathan Kim", None)),
        ("", ("", None)),
    ],
)
def test_split_person_and_agency(value, expected):
    assert split_person_and_agency(value) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("포토그래퍼", "photographer"),
        ("사진", "photographer"),
        ("모델", "model"),
        ("스타일리스트", "stylist"),
        ("헤어", "hair"),
        ("메이크업", "makeup"),
        ("패션 에디터", "fashion_editor"),
        ("에디터", "editor"),
        ("프로덕션", "production"),
        ("듣도보도 못한 역할", None),  # unknown roles stay unnormalized, never guessed
    ],
)
def test_normalize_role(raw, expected):
    assert normalize_role(raw) == expected
