"""Parser tests for the GQ Korea adapter.

PLAN.md §Testing Strategy calls these the highest-value tests in the project:
the parsers are the part that silently breaks when the site is restyled, and
everything downstream trusts what they return.

They run against tests/fixtures/article_pictorial.html: real DOM structure,
invented content, mirroring an article captured 2026-08-23 and re-verified
against six captures spanning all five Style categories and a 2024 page. Every
name and agency asserted below is an invention of that fixture — this repo is
public and the publication's prose and the individuals' names are not ours to
republish. See the comment at the top of that file for the edge cases it
encodes, and for why its container nesting is not negotiable.

One rule earned the hard way, and it applies to the synthetic pages built here
as much as to the fixture: **a fixture is an observation, not an assumption.**
The image-scoping tests below used to pass while the live scraper re-hosted
other articles' thumbnails, because the fixture had been drawn to agree with
the parser rather than with the site. So the synthetic `page()` helper builds
the real div.post_content > div.editor > div.contt chain instead of the flat
shape that is convenient — the flat shape is the one that never occurs.

The corollary is that a fixture can be too *thin* as well as wrong, and this
one was. It carried two of the four editorial container shapes the captures
put inside div.contt, so two whole families of regression went green against
it: widening the chrome denylist to `ul`, `li` or `div.thum` (which erases the
shopping-roundup article entirely) and narrowing the collection rule to
figures, columns and direct children (which loses 18 of the corpus's 44
images). EDITORIAL_SHAPES below names all four, and two tests hold the line —
one that the fixture still contains each shape, one that each shape still
reaches article.images. A guard test that cannot fail is not a guard, which is
also why the two structural raises now carry individually matched messages.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
from selectolax.parser import HTMLParser

from gallery_scraper.sites.gq_korea import (
    CATEGORIES,
    FIRST_PAGE,
    GqKoreaAdapter,
    # Private on purpose: _listing_form's 1-based guard has no public caller
    # that can reach it — _crawl only ever counts up from FIRST_PAGE — so a
    # direct call is the only way to exercise it at all.
    _listing_form,
    normalize_role,
    parse_listing_page,
    split_credit_people,
    split_person_and_agency,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SOURCE_URL = "https://www.gqkorea.co.kr/2026/07/12/summer-black/"

# The eight editorial URLs the fixture carries, in document order, named for
# the container shape each one sits in — so a failure says *which* shape
# stopped being collected instead of only "one image short".
_STEM = "https://img.gqkorea.co.kr/gq/2026/07/style_aaaaaaaaaaaa"
FIGURE_IMAGE = f"{_STEM}1-1400x933.jpg"  # lazy, data-src, appears twice in the body
COLUMN_IMAGE = f"{_STEM}2-1050x1400.jpg"  # not lazy, real URL in src, no twin
FIGURE_WITH_SRCSET = f"{_STEM}3-1400x1867.jpg"  # data-src beside a data-srcset
FIGURE_SRCSET_ONLY = f"{_STEM}4-1400x933.jpg"  # srcset is the only real URL
SHOPPING_IMAGES = (f"{_STEM}5-930x861.jpg", f"{_STEM}6-930x861.jpg")
GALLERY_IMAGES = (f"{_STEM}7-1050x1400.jpg", f"{_STEM}8-1050x1400.jpg")

# The four container shapes that hold editorial images inside div.contt, each
# with the CSS chain the captures print it as and the fixture URLs sitting in
# it. Measured across the six captures, these four account for 44 of 44
# editorial images — figures 24, shopping roundup 10, Swiper gallery 8,
# two-column block 2 — with no remainder. The fixture header records the
# per-capture split; the two tests below keep both halves honest.
EDITORIAL_SHAPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "figure.wp-block-image": (
        "div.contt figure.wp-block-image img",
        (FIGURE_IMAGE, FIGURE_WITH_SRCSET, FIGURE_SRCSET_ONLY),
    ),
    "shopping roundup": (
        "div.contt ul.item_list.shopping_list > li.full.shopping_item > div.thum img",
        SHOPPING_IMAGES,
    ),
    "swiper gallery": (
        "div.contt div.gallery_wrap > div.gallery_slider > div.swiper-wrapper"
        " > div.swiper-slide img",
        GALLERY_IMAGES,
    ),
    "two-column block": (
        "div.contt div.content_columns.two > div.content_column img",
        (COLUMN_IMAGE,),
    ),
}


def fixture_html() -> str:
    return (FIXTURES / "article_pictorial.html").read_text(encoding="utf-8")


def fixture_body():
    """The fixture's div.post_content, for tests that inspect the file itself."""
    body = HTMLParser(fixture_html()).css_first("div.post_content")
    assert body is not None, "the fixture has no div.post_content at all"
    return body


@pytest.fixture(scope="module")
def article():
    return GqKoreaAdapter().parse_article(fixture_html(), SOURCE_URL)


# --------------------------------------------------------------------------
# A minimal page, for the cases the captured fixture cannot express: one
# timestamp per article, and a structure that is *missing* a required part.
# Everything not under test is present and valid, so a failure names one cause.
# --------------------------------------------------------------------------

BREADCRUMB = (
    '<nav aria-label="breadcrumb">'
    # Four crumbs, the last being the article title: that is the live shape on
    # five of the six captures, and the reason _category scans forwards.
    "<ul><li>홈</li><li>STYLE</li><li>pictorial</li>"
    "<li>여름의 끝에서, 검정</li></ul></nav>"
)
TITLE = '<h1 class="post_tit">여름의 끝에서, 검정</h1>'


def well(inner: str) -> str:
    """`inner` inside the real body chain: post_content > editor > contt.

    Every capture nests the editorial content two levels below div.post_content
    and puts chrome at both of those levels, so a synthetic body that omits them
    cannot exercise the scoping at all — which is exactly how the old fixture
    let four pieces of chrome per article through a green suite.
    """
    return (
        '<div class="post_content common_content"><div class="editor">'
        f'<div class="contt">{inner}</div>'
        "</div></div>"
    )


BODY = well('<img src="https://img.gqkorea.co.kr/gq/2026/07/a.jpg">')


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


@pytest.mark.parametrize("title_crumb", ["news", "item", "NEWS", "Item"])
def test_the_title_crumb_never_wins_over_the_subcategory_crumb(title_crumb):
    """The last crumb is the headline, so the scan must not start from the end.

    Measured on all six captures: the trail is [홈, STYLE, <subcategory>,
    <title>] — the sixth drops the subcategory, none of them ends on one. A
    reverse scan therefore tests the *headline* against CATEGORIES first and
    survives only because a headline is rarely one of the five words. Two of
    them are ordinary English words a GQ headline can be made of, and this is
    what happens to the first article titled with one: filed under its own
    title, on a page whose breadcrumb plainly says pictorial, with no error
    anywhere. `.lower()` is applied to each crumb, so the miscategorisation
    does not even need the casing to match.
    """
    nav = (
        '<nav aria-label="breadcrumb"><ul><li>홈</li><li>STYLE</li>'
        f"<li>pictorial</li><li>{title_crumb}</li></ul></nav>"
    )
    assert parse(page(breadcrumb=nav)).category == "pictorial"


def test_a_parent_only_style_article_raises_and_discovery_never_offers_one():
    """The two halves of one invariant, asserted together because they are one.

    A capture from 2024 is filed under the parent term alone: its breadcrumb is
    [홈, STYLE, <title>] with no subcategory, and _category raises on it.
    That is only safe because the *other* half holds — _listing_entry drops the
    same article at discovery, since its post_terms is "STYLE" and CATEGORIES
    holds only the five children — so nothing the parser refuses is ever
    fetched. Neither file can see the other, and each holds half a promise, so
    the promise is pinned in one place: widen the discovery filter without
    touching the parser and this test says which side broke.
    """
    parent_only = (
        '<nav aria-label="breadcrumb"><ul><li>홈</li><li>STYLE</li><li>제목</li></ul></nav>'
    )
    with pytest.raises(ValueError, match="breadcrumb has no known Style category"):
        parse(page(breadcrumb=parent_only))

    payload = {
        "current_term": {"tax1_term": {"name": "STYLE"}},
        "current_posts": [
            {
                "permalink": "https://www.gqkorea.co.kr/2024/01/19/parent-only/",
                "post_date": "2024.01.19",
                "post_id": "272022",
                "post_title": "제목",
                "post_terms": "STYLE",  # the parent, not one of the five children
            }
        ],
    }
    assert parse_listing_page(payload) == ()


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
    # The by-line links exactly one author — the only shape the six captures
    # print. That anchor is also the first /author/ link in document order and
    # it carries text, so this is the path a real page takes.
    assert article.author_name == "김에디터"
    assert article.author_url == "https://www.gqkorea.co.kr/author/김에디터/"


def test_the_unlinked_co_author_is_not_dropped_it_is_a_credit(article):
    """author_name is the *linked* by-line author only. A deliberate choice.

    The fixture's by-line is the observed one: one anchor, then a co-author as
    bare text. Reading the span's text instead of the link would put
    ", 이기자" into author_name, and there is no second href for author_url —
    both are single nullable columns describing one person.

    Nothing is lost, and that is the half of the decision worth pinning: on the
    one capture that has a co-author, div.info_area credits the same name under
    글, so it reaches the database with its role attached. Widening _author
    would duplicate that row into a column that cannot hold a URL for it.
    """
    assert article.author_name == "김에디터"
    assert "이기자" not in article.author_name

    writer = next(c for c in article.credits if c.role_raw == "글")
    assert writer.person_name == "이기자"
    assert writer.role == "writer"


def test_the_fixture_prints_the_by_line_shape_the_captures_print():
    """Guards the fixture — it used to invent a shape reality never produces.

    All six captures put exactly ONE <a href="/author/…"> inside span.author.
    The fixture used to link two authors, which made "first linked author wins"
    look like a real decision about real markup and hid the actual question:
    what happens to a co-author who is not linked at all. If someone restores
    the two-anchor shape, this fails here rather than in the parser.
    """
    span = HTMLParser(fixture_html()).css_first("span.author")
    assert span is not None, "the fixture lost its by-line"

    anchors = span.css('a[href*="/author/"]')
    assert len(anchors) == 1, f"no capture links two authors; fixture links {len(anchors)}"
    assert span.text().strip() != anchors[0].text().strip(), (
        "the by-line's unlinked co-author is gone — the case _author is pinned against"
    )


def test_author_falls_back_to_the_avatar_alt_when_no_link_carries_text():
    """The img-alt branch of _author, pinned deliberately as a *synthetic* case.

    None of the six captures reaches it: the header by-line always comes first
    in document order and always has text. The old fixture had no header at
    all, so its first /author/ anchor was the text-less avatar link and this
    branch looked like the normal path — a code path pinned by an assumption
    rather than an observation, which is the same defect as the image scoping.
    Keeping it as a hand-built page says plainly that it is a safety net for a
    markup change, not a description of the live site.
    """
    profile = (
        '<div class="profile_sub"><div class="profile_pic">'
        '<a href="/author/이기자/" class="author_link">'
        '<img src="https://img/avatar.jpg" alt="이기자"></a>'
        "</div></div>"
    )
    parsed = parse(page(body=well("<p>본문.</p>") + profile))
    assert parsed.author_name == "이기자"
    assert parsed.author_url == "https://www.gqkorea.co.kr/author/이기자/"


def test_source_url_is_preserved(article):
    assert article.source_url == SOURCE_URL


# --------------------------------------------------------------------------
# Credits
# --------------------------------------------------------------------------

def test_credits_are_in_source_order(article):
    # 스타일리스트 appears three times because its one <dd> names three people;
    # see test_a_multi_person_credit_row_becomes_one_credit_per_person.
    assert [c.role_raw for c in article.credits] == [
        "글",
        "포토그래퍼",
        "패션 에디터",
        "스타일리스트",
        "스타일리스트",
        "스타일리스트",
        "모델",
        "헤어",
        "메이크업",
    ]


def test_credit_without_agency(article):
    photographer = next(c for c in article.credits if c.role_raw == "포토그래퍼")
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
    assert by_raw["글"] == "writer"


def test_a_multi_person_credit_row_becomes_one_credit_per_person(article):
    """Three names in one <dd> are three credits, not one string.

    The observed shape, from one capture's stylist row: `이름, 이름, 이름 at
    <에이전시>`. split_person_and_agency cuts only on " at ", so before
    split_credit_people existed all three names went into a single
    person_name — "A, B, C" in a column the v1.1 credit-person filter has to
    match names against, and which no downstream query can take apart again.
    article_credits is one row per person and several rows may share a role,
    so nothing downstream needed changing to hold them.
    """
    stylists = [c for c in article.credits if c.role_raw == "스타일리스트"]
    assert [c.person_name for c in stylists] == ["정하람", "오세빈", "남규현"]
    assert all(c.role == "stylist" for c in stylists)
    assert not any("," in c.person_name for c in article.credits)


def test_the_agency_binds_only_to_the_name_it_is_written_beside(article):
    """The deliberate half of the multi-person decision, pinned so it is visible.

    `A, B, C at <에이전시>` could be read as all three being at that agency —
    plausibly why the editor typed it once rather than three times — and this
    parser rejects that reading: an affiliation the page never printed for A
    and B would land in a column a reader believes, and a null beats a guess
    everywhere else in this module (normalize_role returns None rather than
    guessing). If the call goes the other way, this is the test that has to
    change, and it should change deliberately.
    """
    stylists = [c for c in article.credits if c.role_raw == "스타일리스트"]
    assert [c.agency for c in stylists] == [None, None, "Mureu Studio"]


@pytest.mark.parametrize(
    "value, expected",
    [
        # The observed line: three names, one trailing agency, and it binds to
        # the name it is beside rather than to the list.
        (
            "정하람, 오세빈, 남규현 at Mureu Studio",
            (("정하람", None), ("오세빈", None), ("남규현", "Mureu Studio")),
        ),
        # One person is still one pair — five of the six captures print this.
        ("서윤재", (("서윤재", None),)),
        ("표도현 at 라온엠", (("표도현", "라온엠"),)),
        # Per-part parsing needs no positional rule, so an agency anywhere in
        # the list attaches to its own name. A distribute-the-trailing-agency
        # rule has to invent an answer for both of these.
        ("표도현 at 라온엠, 백서리", (("표도현", "라온엠"), ("백서리", None))),
        (
            "표도현 at 라온엠, 백서리 at 청연에이전시",
            (("표도현", "라온엠"), ("백서리", "청연에이전시")),
        ),
        # Spacing round the comma is the editor's, not the schema's.
        ("정하람 ,오세빈", (("정하람", None), ("오세빈", None))),
        # A trailing or doubled comma is what deleting a name from the middle
        # of a list leaves behind; an empty person_name must not reach the DB.
        ("정하람, , 오세빈,", (("정하람", None), ("오세빈", None))),
        # A blank <dd> yields no credit at all, which is what _credits relies on
        # instead of testing for emptiness itself.
        ("", ()),
        ("   ", ()),
        (",", ()),
    ],
)
def test_split_credit_people(value, expected):
    assert split_credit_people(value) == expected


def test_sponsor_row_is_not_a_person_credit(article):
    # "SPONSORED BY" sits in the same <dl> list as the real credits but names a
    # brand, not a person. Storing it as a person_name would pollute the
    # credit-person filter in v1.1.
    assert all(c.role_raw != "SPONSORED BY" for c in article.credits)


def test_a_source_row_does_not_put_a_hostname_in_person_name(article):
    """출처 ("source") is the syndication row, and its value is a domain.

    One capture prints <dt>출처</dt><dd><a href="…">domain</a></dd> on a
    translated article. The label is in neither _ROLES nor — until now —
    _NON_PERSON_ROLES, so it became a Credit with role None and a hostname in
    person_name: exactly the pollution _NON_PERSON_ROLES exists to prevent,
    and on a row that names a publication, never a person.
    """
    assert all(c.role_raw != "출처" for c in article.credits)
    assert all("example.invalid" not in c.person_name for c in article.credits)


def test_an_organisation_under_a_photo_credit_is_kept_rather_than_guessed_away():
    """A DECISION, not an oversight: the label is screened, the value is not.

    Two captures print an organisation under 사진 — a stock-photo agency on
    one, a phrase naming the brands on the other — and the role normalises to
    photographer, so an organisation does reach person_name. It is left there
    on purpose. Screening 사진 wholesale would drop every real photographer
    credited under the ordinary Korean word for it, and no value-side test
    separates an agency from a person's name without a vocabulary of agency
    words that fails open on the agencies missing from it and fails closed on
    any person whose name contains one. A stored organisation is visible in
    the data and fixable downstream; a dropped photographer is invisible.

    This test exists so the trade is deliberate and so reversing it is a
    deliberate act too — the v1.1 credit-person filter has to treat a
    photographer value as person-or-organisation.
    """
    info = '<div class="info_area"><dl><dt>사진</dt><dd>지어낸이미지코리아</dd></dl></div>'
    credits = parse(page(body=well("<p>본문.</p>") + info)).credits
    assert [(c.role, c.person_name) for c in credits] == [
        ("photographer", "지어낸이미지코리아")
    ]


def test_half_filled_credit_rows_are_skipped_not_stored(article):
    # The fixture carries four rows the CMS prints when a field is left blank: a
    # <dl> with no <dd>, one with no <dt>, a blank value and a blank role. Each
    # would otherwise reach the database as a credit with an empty column — and
    # the two structural ones would raise AttributeError on a None node first.
    assert len(article.credits) == 9  # seven rows, one of which names three
    assert all(c.role_raw and c.person_name for c in article.credits)

    roles = [c.role_raw for c in article.credits]
    assert "프로덕션" not in roles  # its <dl> has no <dd> at all
    assert "어시스턴트" not in roles  # its <dd> is whitespace

    names = {c.person_name for c in article.credits}
    assert "역할이 없는 줄" not in names  # its <dl> has no <dt> at all
    assert "이름만 남은 줄" not in names  # its <dt> is empty


def test_tags_are_not_mistaken_for_credits(article):
    # ul.tag_list is a sibling of the credit <dl>s inside div.info_area.
    names = {c.person_name for c in article.credits}
    assert "화보" not in names and "블랙" not in names


# --------------------------------------------------------------------------
# Body images — the scoping hazard, and the one that actually bit
#
# PLAN.md flagged the hazard and the parser was written against its description
# of it ("div.post_content keeps the author avatar and the recommendation
# modules out"). Six captures say the opposite: both live *inside*
# div.post_content, and only div.contt plus a named exclusion keeps them out.
# The tests below are written against the captures, not against the plan.
# --------------------------------------------------------------------------

def test_only_body_images_are_collected(article):
    # Eight editorial images, spread across all four container shapes the
    # captures use. div.post_content holds 22 <img> in all: those eight, one
    # repeat of the first, ten <noscript> twins, two recommendation thumbnails
    # and the author avatar. The pre-fix div.post_content rule collects 11.
    urls = [i.source_url for i in article.images]
    assert len(urls) == 8, urls


def test_lazy_loaded_url_comes_from_data_src(article):
    urls = [i.source_url for i in article.images]
    assert FIGURE_IMAGE in urls
    assert not any(u.startswith("data:") for u in urls)


def test_non_lazy_image_is_read_from_src(article):
    urls = [i.source_url for i in article.images]
    assert COLUMN_IMAGE in urls


def test_data_src_wins_over_the_srcset_beside_it(article):
    # One body image carries both. data-src is the single full-size URL; the
    # srcset's first candidate happens to be the same file here, but preferring
    # it would make the choice depend on which width GQ lists first.
    urls = [i.source_url for i in article.images]
    assert FIGURE_WITH_SRCSET in urls
    assert "-700x933.jpg" not in " ".join(urls)


def test_srcset_only_image_falls_back_to_its_first_candidate(article):
    # No data-src at all, src a base64 placeholder: without the srcset fallback
    # this image is dropped silently and the article stores one picture short.
    urls = [i.source_url for i in article.images]
    assert FIGURE_SRCSET_ONLY in urls


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
    body = well(f"<img {placeholder} {attrs}>")
    assert [i.source_url for i in parse(page(body=body)).images] == [expected]


def test_a_wider_srcset_candidate_does_not_outrank_data_src():
    """A judgement call, pinned so that changing it has to be a decision.

    The srcset is not merely an alternative spelling of data-src: on 24 of the
    44 editorial images in the six captures it advertises something WIDER.
    data-src is always WordPress's largest derivative — a 1400px long edge on
    31 of the 44 and 930px on 11 — and what beats it is almost always the
    unresized original, which has no -WxH in its filename at all and is sized
    only by its width descriptor (1205w, 1516w, 1829w, 1920w). That is the
    shape the last candidate below stands in for.

    Since images.MAX_EDGE is 1600, taking the best candidate at or above the
    cap would raise all 24 off their 1400px long edge — 21 of them to a full
    1600px, the other three to 1462px — instead of throwing the pixels away:
    ~+31% pixels stored for each of those 21, ~+18% across the whole corpus,
    charged against a 1 GB free tier. And the download is a full-resolution
    master whose byte size the markup never states, so the fetch cost cannot
    be measured from a capture at all — only discovered in production, against
    a site this scraper is trying to be light on. The rendition the page
    serves its own readers wins. If that trade is ever re-made, it gets
    re-made here.
    """
    body = well(
        '<img src="data:image/png;base64,iVBORw0KGgo="'
        ' data-src="https://img/style_a-1050x1400.jpg"'
        ' data-srcset="https://img/style_a-1050x1400.jpg 1050w,'
        ' https://img/style_a-698x930.jpg 698w, https://img/style_a.jpg 1920w">'
    )
    assert [i.source_url for i in parse(page(body=body)).images] == [
        "https://img/style_a-1050x1400.jpg"
    ]


def test_data_src_outranks_a_real_url_sitting_in_src():
    """data-src wins even when src holds a usable URL rather than a placeholder.

    Every lazy <img> on the captured page pairs data-src with a base64 src, so
    the `data:` guard in _image_url masks the attribute order: swap the two and
    the fixture still parses correctly. It stops being equivalent the day GQ
    ships a low-resolution preview in src — the grid would then re-host the
    preview and the full image would never be seen. Pinning the order here is
    what makes the docstring's "data-src wins where present" a promise.
    """
    body = well('<img src="https://img/preview-300.jpg" data-src="https://img/full-1400.jpg">')
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
    body = well(f"<img {attrs}>")
    assert parse(page(body=body)).images == ()


def test_non_figure_editorial_image_is_still_collected(article):
    """An editorial <img> outside figure.wp-block-image must survive the rule.

    Not a hypothetical: the "item" capture is a ten-product roundup with zero
    figure.wp-block-image on the page — all ten of its images live in
    li.shopping_item > div.thum. The "news" capture carries 6 of its 13 in a
    Swiper gallery, and the 2024 one 4 of its 7 across a gallery and a
    two-column block. Scoping to figure.wp-block-image passes every other test
    in this file and silently collects nothing at all from that first article,
    which is the same works-on-one-page mistake in a new costume.

    This one pins the two-column block specifically. The other two non-figure
    shapes are pinned by test_every_editorial_container_shape_reaches_the_gallery.
    """
    urls = [i.source_url for i in article.images]
    assert COLUMN_IMAGE in urls


@pytest.mark.parametrize("shape", list(EDITORIAL_SHAPES))
def test_the_fixture_carries_every_editorial_container_shape(shape):
    """Guards the fixture — it used to carry two of the four, and went green.

    This asserts nothing about the parser. It asserts that the file still has
    all four shapes measured inside div.contt on the captures, because a
    fixture holding only figures and the two-column block cannot see two whole
    classes of regression: adding `ul` or `li` to the chrome denylist erases
    the "item" capture's entire gallery (10 of the corpus's 44 editorial
    images), and narrowing the rule to
    "figure.wp-block-image img, .content_column img, div.contt > img" loses 18
    of 44. Both stayed green against the flattened fixture.
    """
    selector, _ = EDITORIAL_SHAPES[shape]
    assert fixture_body().css(selector), (
        f"the fixture no longer carries the {shape} shape ({selector}); "
        "the captures do, so a rule that drops it would now pass this suite"
    )


@pytest.mark.parametrize("shape", list(EDITORIAL_SHAPES))
def test_every_editorial_container_shape_reaches_the_gallery(article, shape):
    """And the other half: each shape's images must actually be collected.

    The shape being *present* in the fixture is what makes a narrowing rule
    fail; this is where it fails. Split from the guard above so the message
    distinguishes "someone flattened the fixture" from "someone narrowed the
    scoping rule".
    """
    _, expected = EDITORIAL_SHAPES[shape]
    collected = {i.source_url for i in article.images}
    assert set(expected) <= collected, (
        f"the {shape} shape stopped being collected: missing "
        f"{sorted(set(expected) - collected)}"
    )


def test_div_thum_is_shared_by_editorial_and_recommendation_images():
    """The trap inside the shopping-roundup shape, guarded in the fixture.

    div.thum looks like a chrome marker because the relate_group's thumbnails
    live in one. On the "item" capture it is also the wrapper around all ten
    editorial product shots, so blocklisting it costs that article its whole
    gallery. Both uses are in the fixture, in one document, so the mistake
    fails here — the class alone decides nothing; the container it sits in does.
    """
    body = fixture_body()
    assert body.css("div.contt .relate_group div.thum img"), "no chrome div.thum left"
    assert body.css("div.contt li.shopping_item > div.thum img"), (
        "no editorial div.thum left — blocklisting div.thum would now pass"
    )


def test_chrome_and_recommendations_are_excluded(article):
    urls = " ".join(i.source_url for i in article.images)
    assert "facebook.com" not in urls          # tracking pixel
    assert "logo.svg" not in urls              # site chrome
    assert "bbbbbbbbbbbb1" not in urls         # author avatar, inside post_content
    assert "cccccccccccc1" not in urls         # in-body recommendation module
    assert "cccccccccccc2" not in urls
    assert "cccccccccccc9" not in urls         # the MUST READ aside, outside it


def test_the_fixture_really_does_nest_the_chrome_inside_the_body():
    """Guards the fixture itself — the thing that failed last time.

    This test asserts nothing about the parser. It asserts that the fixture
    still carries the two containers *where the live pages put them*, because
    the previous fixture put both outside div.post_content and thereby turned
    test_recommendation_module_inside_the_body_is_excluded and its avatar twin
    into tests that pass no matter what the parser does. If someone
    "simplifies" the fixture back to the flat shape, this fails first and says
    why, instead of the suite going quietly green over a broken scraper.
    """
    body = fixture_body()

    # The two nesting levels every capture has and the old fixture had neither of.
    assert body.css_first("div.editor") is not None
    assert body.css_first("div.editor div.contt") is not None
    # The recommendation module: inside the content well, not beside the article.
    assert body.css_first("div.contt div.relate_group") is not None
    # The author avatar: a direct child of post_content, sibling of div.editor.
    assert body.css_first("div.profile_sub div.profile_pic img") is not None
    # The lazy-image twins, absent from the old fixture entirely.
    assert body.css("div.contt noscript"), "no <noscript> twins left in the body"
    # Which of the four editorial shapes are present is guarded separately, by
    # test_the_fixture_carries_every_editorial_container_shape.


def test_recommendation_module_inside_the_body_is_excluded(article):
    """The defect a live --dry-run found and 337 green tests did not.

    div.relate_group.relate_content is nested inside div.contt — inside
    div.editor, inside div.post_content — on five of the six captures. Its
    thumbnails belong to *other* articles, and collecting them means the
    pipeline downloads, re-hashes and re-hosts a stranger's picture into this
    article's gallery, then bills it against the 1 GB free tier.
    """
    urls = [i.source_url for i in article.images]
    assert not any("500x500" in u for u in urls), urls
    assert not any("/2026/06/" in u for u in urls), urls


def test_author_avatar_inside_the_body_is_excluded(article):
    """div.profile_sub is a direct child of div.post_content on all six captures.

    The avatar is 600x600 and, on advertorials, a single shared theme
    placeholder — so an over-broad rule re-hosts the same file once per article,
    forever, and appends it to every gallery as a body image.
    """
    urls = [i.source_url for i in article.images]
    assert not any("bbbbbbbbbbbb" in u for u in urls), urls


def test_repeated_image_is_collected_once(article):
    # The same URL appears twice in the body. A single INSERT carrying both
    # would fail on unique (article_id, content_hash) with cardinality_violation
    # — supabase/README.md records this as a pipeline requirement.
    urls = [i.source_url for i in article.images]
    assert len(urls) == len(set(urls))


def test_image_positions_are_1_based_and_contiguous(article):
    assert [i.position for i in article.images] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_noscript_twin_is_not_collected_as_a_second_image():
    """The <noscript> fallback beside every lazy image must not be counted.

    On the captures the twin always carries a byte-identical URL — 57 pairs,
    zero mismatches — so the URL dedupe happens to collapse it and no test ever
    saw the shape. That is the dedupe doing load-bearing work it was not
    written for: it holds only while the live <img> precedes its twin and the
    twin serves the same crop. Here the twin carries a *different* rendition,
    which is what a theme change would produce, and it must still be one image.
    """
    figure = (
        '<figure class="wp-block-image size-large">'
        '<img class="lazyload" src="data:image/png;base64,iVBORw0KGgo="'
        ' data-src="https://img/full-1400.jpg">'
        "<noscript>"
        '<img src="https://img/full-1400-DIFFERENT-CROP.jpg">'
        "</noscript>"
        "</figure>"
    )
    assert [i.source_url for i in parse(page(body=well(figure))).images] == [
        "https://img/full-1400.jpg"
    ]


def test_selectolax_nodes_compare_structurally_but_hash_by_identity():
    """The measurement the chrome-exclusion comment rests on. Measured here so
    it cannot rot into folklore, as the previous comment did — it claimed nodes
    "compare and hash by the underlying element", which is half true and licenses
    exactly the wrong refactor.

    selectolax 0.4.11: `==` is structural, so two *different* <img> elements
    with identical markup are equal; `hash()` is identity-based, so those same
    two hash differently. A set therefore buckets by hash before it ever calls
    `==` and only ever matches the very node that was put in it. A list has no
    such shortcut and matches on `==` alone.
    """
    first, second = HTMLParser('<div><img src="a.jpg"><img src="a.jpg"></div>').css("img")

    assert first == second, "== is structural: identical markup compares equal"
    assert hash(first) != hash(second), "hash() is identity: distinct elements differ"
    assert first not in {second}, "a set asks hash first — this is the behaviour relied on"
    assert first in [second], "a list asks == only — this is the over-exclusion"


def test_an_editorial_image_matching_chrome_markup_is_still_collected():
    """`chrome` must stay a set. As a list this returns nothing.

    The exclusion is a membership test against nodes, and it is correct only
    because `chrome` is a set: `img in chrome` then means "is this the very
    <img> I collected from a chrome wrapper?". Switch it to a list and the test
    becomes structural equality, so an editorial image whose markup happens to
    match a recommendation thumbnail — same file re-used as its own teaser,
    which is how a lot of galleries are built — is silently dropped.

    Both <img> below are byte-identical, one editorial and one inside the
    relate_group, and the editorial one comes first, so the URL dedupe cannot
    mask the difference: a set yields one image, a list yields none.
    """
    twin = '<img src="https://img/shared.jpg">'
    body = well(f'{twin}<div class="relate_group"><div class="thum">{twin}</div></div>')
    assert [i.source_url for i in parse(page(body=body)).images] == ["https://img/shared.jpg"]


def test_missing_body_container_raises_like_every_other_required_field():
    """div.post_content is one renamed class away in a theme update.

    Returning () there yields articles that parse cleanly, store fine and have
    no images at all — a whole run of them, behind no error and no failed count.

    The regex is anchored on this branch's own wording on purpose. It used to
    be `match="post body"`, which the *other* raise's message also contains, so
    replacing this guard with `tree.css_first(_POST_CONTENT) or tree.root` —
    deleting the raise and silently widening the scope, exactly the regression
    this test is named for — left the whole suite green: the well lookup then
    failed instead and its message matched.
    """
    with pytest.raises(ValueError, match=r"^no div\.post_content post body found"):
        parse(page(body='<div class="entry-content"><img src="https://img/a.jpg"></div>'))


def test_the_post_body_selector_is_not_satisfied_by_the_inner_editor_div():
    """_POST_CONTENT is the tripwire for a renamed theme class, so pin it.

    div.editor and div.contt sit inside div.post_content on all six captures,
    so repointing _POST_CONTENT at either still finds a well and still collects
    the right images from every capture — the rename goes unnoticed and the
    outermost container stops being checked at all. A page that has the inner
    chain but not the wrapper must still be a structural surprise, and the
    message must still name the selector that was not found.
    """
    body = '<div class="editor"><div class="contt"><img src="https://img/a.jpg"></div></div>'
    with pytest.raises(ValueError, match=r"^no div\.post_content post body found"):
        parse(page(body=body))


def test_missing_content_well_raises_rather_than_widening_the_net():
    """div.contt is the allowlist; losing it must stop the run, not relax it.

    The tempting fallback is "no div.contt? use div.post_content then" — and
    that is precisely the rule this whole change exists to remove. It would
    turn a renamed class into a silent return to collecting recommendation
    thumbnails and avatars, on every article, with nothing in the logs.
    """
    body = '<div class="post_content"><div class="editor"><img src="https://img/a.jpg"></div></div>'
    # Anchored on the whole message, which names _CONTENT_WELL: a bare
    # `match="div.contt"` also accepts the missing-post-body branch, and
    # accepts a _CONTENT_WELL repointed to anything containing that substring.
    with pytest.raises(ValueError, match=r"^post body has no div\.contt content well"):
        parse(page(body=body))


def test_the_content_well_is_searched_inside_the_post_body_not_page_wide():
    """The well must be a *descendant* of the body, and nothing else pins that.

    Every capture has exactly one div.contt, so resolving it against the whole
    document instead of against div.post_content passes all six and every other
    test here. It stops being equivalent the first time the theme prints a
    div.contt earlier in the page — a sidebar, a module hydrated server-side —
    at which point that one silently becomes the article's gallery.
    """
    decoy = '<div class="contt"><img src="https://img/decoy.jpg"></div>'
    parsed = parse(page(body=decoy + well('<img src="https://img/real.jpg">')))
    assert [i.source_url for i in parsed.images] == ["https://img/real.jpg"]


def test_a_body_container_with_no_images_is_not_an_error():
    # The other side of that line: the container is there and holds no images,
    # which is a text-only article, not a structural surprise. It parses, stores
    # and — with zero images, so zero image failures — completes normally.
    parsed = parse(page(body=well("<p>본문만 있는 기사.</p>")))
    assert parsed.images == ()
    assert parsed.title == "여름의 끝에서, 검정"


# --------------------------------------------------------------------------
# Helpers, unit level
# --------------------------------------------------------------------------

@pytest.mark.parametrize("paged", [0, -1, -50])
def test_listing_form_rejects_a_page_below_the_first(paged):
    """`paged` is 1-based at the endpoint, and page 0 is not the first page.

    Nothing in the adapter can produce one — _crawl counts from FIRST_PAGE — so
    this guard exists for the next caller, and an unexercised guard is a guard
    nobody has read. A zero here would be sent as `paged=0`, and the endpoint
    answers that with a page whose contents no caller expects.
    """
    with pytest.raises(ValueError, match="1-based"):
        _listing_form(paged)


def test_listing_form_accepts_the_first_page_and_sends_strings():
    # The other side of the boundary: FIRST_PAGE itself is valid. Every value
    # in the body is a string by contract — the endpoint is form-encoded.
    form = _listing_form(FIRST_PAGE)
    assert form["paged"] == "1"
    assert all(isinstance(value, str) for value in form.values())


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

# --------------------------------------------------------------------------
# Selector scope
#
# The tests below split into two kinds, and the distinction matters enough to
# state. The ones reading `article` pin behaviour against structure the six
# captures actually have. The ones building HTML inline pin *defensive* scoping
# — the code deliberately narrows a selector, and these prove the narrowing
# holds. Their shapes are NOT claimed to occur on gqkorea.co.kr, and each says
# so, because a fixture that quietly asserts an invented shape is representative
# is the exact mistake this file has already made twice.
# --------------------------------------------------------------------------

def test_the_fixture_carries_both_navs_the_captures_carry():
    """Guard on the fixture, not the parser.

    All six captures print a site menu before the breadcrumb, and that menu
    lists every category. Delete it and `_category`'s selector stops being
    observable: a rule taking any nav would read the breadcrumb anyway and the
    suite would stay green while every live article filed as grooming.
    """
    tree = HTMLParser(fixture_html())
    navs = tree.css("nav")
    assert len(navs) >= 2, "the site menu is missing — see the comment on this test"
    assert navs[0].attributes.get("aria-label") != "breadcrumb", (
        "the breadcrumb must not be the first nav, or the selector is untested"
    )
    menu = {" ".join(li.text().split()) for li in navs[0].css("li")}
    assert set(CATEGORIES) <= menu, "the menu must advertise every category"


def test_the_site_menu_does_not_win_over_the_breadcrumb(article):
    # Mutating nav[aria-label="breadcrumb"] to a bare nav reads "grooming" here,
    # the first category the menu lists, exactly as it would on every capture.
    assert article.category == "pictorial"


def test_a_definition_list_outside_info_area_is_not_a_credit():
    """`_credits` scopes to div.info_area; this proves the scope is load-bearing.

    No capture prints a <dl> outside div.info_area — page-wide counts are
    5/2/1/1/1/1 and every one is inside it. So this shape is synthetic on
    purpose: it stands in for a spec table or a footer definition list arriving
    in a theme update, which without the scope would be stored as credits with
    a product attribute in person_name.
    """
    body = (
        '<div class="post_content"><div class="editor"><div class="contt">'
        "<dl><dt>소재</dt><dd>코튼 100%</dd></dl>"
        "</div></div>"
        '<div class="info_area"><dl><dt>포토그래퍼</dt><dd>서윤재</dd></dl></div>'
        "</div>"
    )
    credits = parse(page(body=body)).credits
    assert [c.role_raw for c in credits] == ["포토그래퍼"]


def test_the_headline_wins_over_og_title():
    """og:title is byte-identical to h1.post_tit on all six captures.

    So the preference order the code expresses — h1 authoritative, og:title only
    as a fallback — is invisible from the fixture, and reversing it passes.
    These two synthetic pages are the only place the order is observable.
    """
    html = page(title='<h1 class="post_tit">머리말</h1>').replace(
        "<head>", '<head><meta property="og:title" content="다른 제목">'
    )
    assert parse(html).title == "머리말"


def test_og_title_is_used_only_when_the_headline_is_missing():
    html = page(title="").replace(
        "<head>", '<head><meta property="og:title" content="대체 제목">'
    )
    assert parse(html).title == "대체 제목"


def test_a_headline_outside_post_tit_does_not_become_the_title():
    """Every capture has exactly one <h1>, so `h1.post_tit` vs a bare `h1` is
    unobservable there. A section heading is the plausible second one."""
    html = page(title='<h1 class="section_tit">섹션 머리말</h1>').replace(
        "<head>", '<head><meta property="og:title" content="진짜 제목">'
    )
    assert parse(html).title == "진짜 제목"
