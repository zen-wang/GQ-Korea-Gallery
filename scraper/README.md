# scraper/

Python pipeline that turns GQ Korea Style articles into rows in Supabase and
objects in the `gallery` Storage bucket.

```bash
uv venv .venv && uv pip install --python .venv/bin/python selectolax pytest
PYTHONPATH=. ./.venv/bin/python -m pytest tests/ -q
```

## Site reconnaissance (2026-08-23)

Findings from probing the live site. Several contradict PLAN.md's original
assumptions, which were made before anyone had looked at the markup.

**`robots.txt` permits what we do, and we stay inside it.** The `User-agent: *`
group disallows only `/wp-admin/`, `/search/`, `/preview/` and `/auth/`; article
and category paths are open. Two other groups matter for etiquette rather than
permission: named crawlers (Amazonbot, CCBot, **Scrapy**, …) and AI training
crawlers (GPTBot, **ClaudeBot**, Google-Extended, …) are disallowed outright.
We are none of those — this is a personal archive fetching its own reading
material, not a crawler and not training data — but the signal is clear enough
that politeness is not optional here: identify honestly in the UA, rate-limit,
back off, and never touch the four disallowed paths.

**No browser needed for articles.** PLAN.md assumed Playwright because body
images are lazy-loaded, and they are — `src` carries a base64 placeholder. But
the real URL ships in `data-src` in the raw HTML, so a plain HTTP GET is enough.
That removes a heavy dependency from the per-article path and means one request
per article instead of a headless browser session.

**Discovery still needs rendering.** Category grids are hydrated client-side:
`/category/style/`, `page/2/` and `page/3/` all return the same 17 permalinks,
which belong to a static "recommended" module rather than the grid. Options, in
order of politeness: the `admin-ajax.php` endpoint `robots.txt` explicitly
allows; the sitemap; a rendered page.

**The WordPress REST API is closed.** `/wp-json/` and `/wp-json/wp/v2/posts`
both return 401 `rest_not_logged_in`, so the structured shortcut is unavailable.

**A sitemap exists and is advertised.** `robots.txt` points at `/sitemap.xml`,
which redirects to `/wp-sitemap.xml` — an index of 14 post sitemaps. URLs are
date-based (`/2026/08/23/<slug>/`) and carry no category, so the sitemap alone
cannot filter to Style; it is however the cheapest way to spot *new* articles
for incremental runs.

**Category subpaths are not what PLAN.md guessed.** `/category/style/` works;
`/category/style/pictorial/` returns 404 even though the taxonomy sitemap lists
it. This costs nothing: PLAN.md already treats the article's own breadcrumb as
the authoritative category, and it is the only element on the page describing
*that* article rather than a recommendation module.

## Parsers

`sites/gq_korea.py` is pinned by `tests/fixtures/article_pictorial.html`, whose
DOM mirrors a real article with synthetic text — this repo is public, so the
publication's prose is not reproduced in it. The parser is also validated
against a real captured page during development.

Selectors that matter, and what breaks if the site changes:

| Field | Selector | Note |
|---|---|---|
| category | `nav[aria-label=breadcrumb]` last known crumb | authoritative; recommendation modules advertise other categories |
| title | `h1.post_tit`, falling back to `og:title` | |
| date | `meta[property=article:published_time]` | UTC in the markup, converted to KST before taking the date |
| author | first `a[href*="/author/"]` | avatar link carries the name in `img[alt]`, not text |
| credits | `div.info_area > dl > dt/dd` | `ul.tag_list` is a sibling; scoping to `dl` excludes it |
| images | `div.post_content img`, `data-src` then `src` | scoping is what excludes avatars and MUST READ |

## Left to build

- `discover_article_urls` — see the discovery note above.
- `images.py` — download, resize to <= 1600px, WebP plus a thumbnail.
- `db.py` upserts, and `pipeline.py` wiring it together.

Two constraints the schema imposes on the pipeline, recorded in
`../supabase/README.md`: collapse duplicate images in memory before sending a
batch, and treat `images` as insert-or-update rather than delete-and-recreate,
because reactions and list entries cascade off `images.id`.
