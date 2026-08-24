# scraper/

Python pipeline that turns GQ Korea Style articles into rows in Supabase and
objects in the `gallery` Storage bucket.

```bash
uv venv .venv
uv pip install --python .venv/bin/python selectolax httpx pillow supabase pytest pytest-cov
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

**Discovery needs no browser either — `robots.txt` hands us an endpoint.**
Category grids are hydrated client-side (`/category/style/`, `page/2/` and
`page/3/` all return the same 17 permalinks, from a static "recommended" module
rather than the grid), so this looked like the one place Playwright was
unavoidable. It is not: the `User-agent: *` group disallows `/wp-admin/` and
then adds `Allow: /wp-admin/admin-ajax.php`, and the grid's own loader posts
`action=get_posts_1depth_list` there. See `## Discovery` below. **Playwright is
therefore not a dependency of this project at all.**

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

## Discovery

`POST /wp-admin/admin-ajax.php`, form-encoded, returns JSON:

    action=get_posts_1depth_list  post_type=post  tax1_slug=style
    posts_per_page=50  paged=<1-based>  notInPosts=0

Each post carries `permalink`, `post_date`, `post_id`, `post_title`,
`post_terms`, `post_editors`, `post_thumbnail_url` — so the subcategory arrives
with the listing and discovery never fetches an article just to learn what it
is. Four behaviours were confirmed by probing, and each is load-bearing:

- **`notInPosts` must be present and non-empty** or the response omits
  `current_posts` entirely. We send `0`, a post id that cannot exist. The page's
  own JS hardcodes 17 "recommended" ids there; copying that would skip them.
- **End of list is the `current_posts` key being absent**, not an empty array.
  GQ's own JS reads `data['current_posts'].length` unguarded and would throw
  there, so their termination check is not one we can borrow.
- **`post_terms` is sometimes the literal `STYLE`** — an article filed under the
  parent with no subcategory. `article_category` is an enum of the five
  lowercase subcategories, so those are dropped at discovery rather than
  raising on insert.
- **A renamed taxonomy answers `tax1_term: false`**, and a page-1 response with
  no posts means the endpoint stopped accepting our parameters. Both raise: a
  taxonomy holding ~7,450 posts is never legitimately empty on page 1, and
  silence there would read as "0 new articles" forever.

## Incremental runs, and why `content_hash` is the completion marker

`articles.content_hash` is written **NULL** by `upsert_article` and filled in by
`mark_article_complete` only once every image of that article has landed. So
"we already have this one" is `db.completed_source_urls`, not "a row exists".

That distinction is the whole incremental strategy, and getting it wrong is
silent and permanent. If `seen` meant "a row exists", then a run capped by
`--max-articles`, or killed by the job timeout, or one whose images all failed
against a 403-ing CDN, would leave rows for the newest articles and nothing for
the rest — and because the listing is newest-first, every later run would stop
at the first stored permalink and never reach them again. A capped backfill
would make no progress after its first chunk.

Discovery matches: `_crawl` **skips** seen entries rather than returning at the
first one, and stops only when a whole page yields nothing unseen. Steady state
costs one extra listing POST per run; in exchange every hole — capped runs,
timeouts, part-failed articles, an offset-drift skip — heals on the next run.

## Modules

| Module | What |
|---|---|
| `core/http.py` | `PoliteClient`: honest UA, rate limit, backoff, `Retry-After`, size guards |
| `core/adapter.py` | `SiteAdapter` protocol, `ArticleData` / `Credit` / `ImageRef` / `ListingEntry` |
| `sites/gq_korea.py` | discovery + article parsers, the only GQ-specific file |
| `images.py` | download, EXIF-orient, ≤1600px WebP + 600px thumb, bomb guard |
| `storage.py` | uploads to the `gallery` bucket, idempotent |
| `db.py` | upserts, the completion marker, truncation-immune paging |
| `pipeline.py` | the run loop, tallies, health check, CLI |

```bash
python -m gallery_scraper.pipeline --help
python -m gallery_scraper.pipeline --dry-run --max-articles 2   # writes nothing
```

A run exits non-zero when every attempted article failed (the article template
moved) or when images were attempted and none succeeded (the CDN or the storage
quota). Finding nothing new is the ordinary incremental outcome and exits 0.

## Fixtures

`tests/fixtures/` carries **real DOM structure with invented content**. This
repo is public: the publication's prose and the individuals' names are not ours
to republish, and a fixture that quietly carried real by-lines while claiming
otherwise would be worse than one that made no claim. Every person and agency
in `article_pictorial.html` and `listing_style_page1.json` is made up.
