"""Parser tests for the GQ Korea adapter.

PLAN.md §Testing Strategy calls these the highest-value tests in the project:
the parsers are the part that silently breaks when the site is restyled, and
everything downstream trusts what they return.

They run against tests/fixtures/article_pictorial.html: real DOM structure,
invented content, mirroring an article captured 2026-08-23. Every name and
agency asserted below is an invention of that fixture — this repo is public and
the publication's prose and the individuals' names are not ours to republish.
See the comment at the top of that file for the edge cases it encodes.
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
# A minimal page, for the cases the captured fixture cannot express: one
# timestamp per article, and a structure that is *missing* a required part.
# Everything not under test is present and valid, so a failure names one cause.
# --------------------------------------------------------------------------

BREADCRUMB = (
    '<nav aria-label="breadcrumb">'
    "<ul><li>홈</li><li>STYLE</li><li>pictorial</li></ul></nav>"
)
TITLE = '<h1 class="post_tit">여름의 끝에서, 검정</h1>'
BODY = '<div class="post_content"><img src="https://img.gqkorea.co.kr/gq/2026/07/a.jpg"></div>'


def page(
    *,
    published_time: str | None = None,
    breadcrumb: str = BREADCRUMB,
    title: str = TITLE,
    body: str = BODY,
) -> str:
    """One synthetic article page, in the shape article_pictorial.html captures."""
    meta = ""
    if published_time is not None:
        meta = f'<meta property="article:published_time" content="{published_time}">'
    return f"<html><head>{meta}</head><body>{breadcrumb}{title}{body}</body></html>"


def parse(html: str):
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


@pytest.mark.parametrize(
    "published_time, expected",
    [
        # 00:30 KST on the 12th, stamped 15:30Z on the 11th. This is the case
        # the module docstring is written about, and the one the fixture's own
        # 03:00Z timestamp cannot catch: taking the UTC date files it a day early.
        ("2026-07-11T15:30:00+00:00", dt.date(2026, 7, 12)),
        # The same instant seen from the other side of the date line: a stamp
        # whose printed date is a day *ahead* of the Seoul date must come back.
        ("2026-07-13T02:00:00+13:00", dt.date(2026, 7, 12)),
        # Offsets other than UTC turn up whenever a CMS is reconfigured.
        ("2026-07-12T20:00:00-04:00", dt.date(2026, 7, 13)),
        # No offset at all: read as UTC, then converted like any other stamp.
        ("2026-07-11T15:30:00", dt.date(2026, 7, 12)),
        # 22:00 KST, stamped 13:00Z the same day — the control that stays put.
        ("2026-07-12T13:00:00+00:00", dt.date(2026, 7, 12)),
    ],
)
def test_published_date_is_the_seoul_date_not_the_stamped_one(published_time, expected):
    assert parse(page(published_time=published_time)).published_date == expected


@pytest.mark.parametrize("published_time", [None, "", "not a timestamp"])
def test_published_date_degrades_to_none_when_the_stamp_is_unusable(published_time):
    # published_date is nullable in the schema; losing the article over a
    # malformed meta tag would cost far more than losing its date.
    assert parse(page(published_time=published_time)).published_date is None


# --------------------------------------------------------------------------
# Structural surprises — the run's only markup alarm
#
# These raises are what turn a restyle into a failed run instead of a quiet one.
# Without a test each, "return a default instead" survives the whole suite.
# --------------------------------------------------------------------------

def test_missing_breadcrumb_raises_rather_than_defaulting_a_category():
    with pytest.raises(ValueError, match="breadcrumb"):
        parse(page(breadcrumb=""))


def test_breadcrumb_without_a_known_category_raises_and_shows_the_crumbs():
    # The nav survived a restyle but its leaf is something else now. Guessing
    # would file every article of the run under one wrong category.
    nav = '<nav aria-label="breadcrumb"><ul><li>홈</li><li>STYLE</li><li>워치</li></ul></nav>'
    with pytest.raises(ValueError, match="워치"):
        parse(page(breadcrumb=nav))


def test_missing_title_raises_when_neither_the_h1_nor_og_title_is_there():
    with pytest.raises(ValueError, match="title"):
        parse(page(title=""))


def test_title_falls_back_to_og_title_when_the_h1_is_gone():
    with_og = page(title='<meta property="og:title" content="여름의 끝에서, 검정">')
    assert parse(with_og).title == "여름의 끝에서, 검정"


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
    assert photographer.person_name == "서윤재"
    assert photographer.agency is None


def test_credit_with_agency_is_split(article):
    model = next(c for c in article.credits if c.role_raw == "모델")
    assert model.person_name == "표도현"
    assert model.agency == "라온엠"


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


def test_half_filled_credit_rows_are_skipped_not_stored(article):
    # The fixture carries four rows the CMS prints when a field is left blank: a
    # <dl> with no <dd>, one with no <dt>, a blank value and a blank role. Each
    # would otherwise reach the database as a credit with an empty column — and
    # the two structural ones would raise AttributeError on a None node first.
    assert len(article.credits) == 5
    assert all(c.role_raw and c.person_name for c in article.credits)

    roles = [c.role_raw for c in article.credits]
    assert "스타일리스트" not in roles  # its <dl> has no <dd> at all
    assert "어시스턴트" not in roles  # its <dd> is whitespace

    names = {c.person_name for c in article.credits}
    assert "역할이 없는 줄" not in names  # its <dl> has no <dt> at all
    assert "이름만 남은 줄" not in names  # its <dt> is empty


def test_tags_are_not_mistaken_for_credits(article):
    # ul.tag_list is a sibling of the credit <dl>s inside div.info_area.
    names = {c.person_name for c in article.credits}
    assert "화보" not in names and "블랙" not in names


# --------------------------------------------------------------------------
# Body images — the scoping hazard PLAN.md flags
# --------------------------------------------------------------------------

def test_only_body_images_are_collected(article):
    urls = [i.source_url for i in article.images]
    assert len(urls) == 4, urls


def test_lazy_loaded_url_comes_from_data_src(article):
    urls = [i.source_url for i in article.images]
    assert "https://img.gqkorea.co.kr/gq/2026/07/style_aaaaaaaaaaaa1-1400x933.jpg" in urls
    assert not any(u.startswith("data:") for u in urls)


def test_non_lazy_image_is_read_from_src(article):
    urls = [i.source_url for i in article.images]
    assert "https://img.gqkorea.co.kr/gq/2026/07/style_aaaaaaaaaaaa2-1050x1400.jpg" in urls


def test_data_src_wins_over_the_srcset_beside_it(article):
    # One body image carries both. data-src is the single full-size URL; the
    # srcset's first candidate happens to be the same file here, but preferring
    # it would make the choice depend on which width GQ lists first.
    urls = [i.source_url for i in article.images]
    assert "https://img.gqkorea.co.kr/gq/2026/07/style_aaaaaaaaaaaa3-1400x1867.jpg" in urls
    assert "-700x933.jpg" not in " ".join(urls)


def test_srcset_only_image_falls_back_to_its_first_candidate(article):
    # No data-src at all, src a base64 placeholder: without the srcset fallback
    # this image is dropped silently and the article stores one picture short.
    urls = [i.source_url for i in article.images]
    assert "https://img.gqkorea.co.kr/gq/2026/07/style_aaaaaaaaaaaa4-1400x933.jpg" in urls


@pytest.mark.parametrize(
    "attrs, expected",
    [
        # The srcset is a comma-separated list of "url width" pairs; the first
        # candidate is the one to take, and the width descriptor is not part of
        # the URL. A plain `srcset` is the same fallback for a non-lazy image.
        (
            'srcset="https://img/a-1400.jpg 1400w, https://img/a-700.jpg 700w"',
            "https://img/a-1400.jpg",
        ),
        ('data-srcset="https://img/a-1400.jpg 1400w"', "https://img/a-1400.jpg"),
        ('data-srcset="https://img/a-1400.jpg"', "https://img/a-1400.jpg"),  # no descriptor
        # data-srcset outranks srcset for the same reason data-src outranks src.
        (
            'data-srcset="https://img/lazy.jpg 1400w" srcset="https://img/eager.jpg 1400w"',
            "https://img/lazy.jpg",
        ),
    ],
)
def test_image_url_falls_back_through_the_srcset_attributes(attrs, expected):
    placeholder = 'src="data:image/png;base64,iVBORw0KGgo="'
    body = f'<div class="post_content"><img {placeholder} {attrs}></div>'
    assert [i.source_url for i in parse(page(body=body)).images] == [expected]


def test_data_src_outranks_a_real_url_sitting_in_src():
    """data-src wins even when src holds a usable URL rather than a placeholder.

    Every lazy <img> on the captured page pairs data-src with a base64 src, so
    the `data:` guard in _image_url masks the attribute order: swap the two and
    the fixture still parses correctly. It stops being equivalent the day GQ
    ships a low-resolution preview in src — the grid would then re-host the
    preview and the full image would never be seen. Pinning the order here is
    what makes the docstring's "data-src wins where present" a promise.
    """
    body = (
        '<div class="post_content">'
        '<img src="https://img/preview-300.jpg" data-src="https://img/full-1400.jpg">'
        "</div>"
    )
    assert [i.source_url for i in parse(page(body=body)).images] == [
        "https://img/full-1400.jpg"
    ]


@pytest.mark.parametrize(
    "attrs",
    [
        "",                                              # no URL-bearing attribute at all
        'src="data:image/png;base64,iVBORw0KGgo="',      # placeholder and nothing else
        'src="data:image/gif;base64,R0lGOD=" srcset="data:image/gif;base64,R0lGOD= 1x"',
        'data-srcset="   "',                             # present but empty
    ],
)
def test_an_image_with_no_real_url_is_dropped_rather_than_stored(attrs):
    # A base64 placeholder stored as images.source_url would be fetched by the
    # downloader as a URL and fail every retry, once per article, forever.
    body = f'<div class="post_content"><img {attrs}></div>'
    assert parse(page(body=body)).images == ()


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
    assert [i.position for i in article.images] == [1, 2, 3, 4]


def test_missing_body_container_raises_like_every_other_required_field():
    # div.post_content is one renamed class away in a theme update. Returning ()
    # there yields articles that parse cleanly, store fine and have no images at
    # all — a whole run of them, behind no error and no failed count.
    with pytest.raises(ValueError, match="post body"):
        parse(page(body='<div class="entry-content"><img src="https://img/a.jpg"></div>'))


def test_a_body_container_with_no_images_is_not_an_error():
    # The other side of that line: the container is there and holds no images,
    # which is a text-only article, not a structural surprise. It parses, stores
    # and — with zero images, so zero image failures — completes normally.
    parsed = parse(page(body='<div class="post_content"><p>본문만 있는 기사.</p></div>'))
    assert parsed.images == ()
    assert parsed.title == "여름의 끝에서, 검정"


# --------------------------------------------------------------------------
# Helpers, unit level
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("표도현 at 라온엠", ("표도현", "라온엠")),
        ("서윤재", ("서윤재", None)),
        ("  여도하  at  청연에이전시  ", ("여도하", "청연에이전시")),
        ("백서리 AT 하람컴퍼니", ("백서리", "하람컴퍼니")),
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
