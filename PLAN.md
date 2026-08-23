# Aesthetic Gallery — Fashion Image Scraper & Gallery

## Context

**Goal:** Build a private, aesthetic image-gallery website that scrapes fashion editorial sites (starting with **GQ Korea**), tags/attributes every image, lets a small group browse with filters, react (like/dislike), and curate Pinterest-style lists — and keeps itself fresh by re-scraping when sources publish new content.

**Why:** A personal, curated visual reference tool for fashion/editorial imagery, richer than bookmarking because every image carries structured metadata (category, date, author, full photo credits, source link) and personal curation (reactions, lists).

**Outcome:** A static React gallery on **GitHub Pages**, backed by **Supabase** (Postgres + Auth) and **Cloudflare R2** (image files), fed by a **Python scraper running on GitHub Actions cron**. v1 scope = GQ Korea **Style** tab only (grooming, item, news, pictorial, sneakers), but the scraper is built pluggable so more sites drop in later.

---

## Decisions Locked (from brainstorming)

| Decision | Choice |
|---|---|
| Audience | **Me + a few friends** → invite-only auth, per-user reactions/lists |
| User data | **Supabase free backend** (cross-device sync) |
| Image files | **Cloudflare R2** (10GB free, zero egress) — re-hosted/downloaded |
| Site hosting | **GitHub Pages** (all-GitHub) |
| v1 scrape scope | **GQ Korea "Style" tab** (5 types), pluggable for more |
| Aesthetic | **Clean editorial grid + smooth motion** (fade/slide-up reveals, gradual text appearance) |
| Scraper language | **Python** (best scraping ecosystem; Node noted as alt) |
| GitHub account | **github.com/zen-wang** — repo `GQ-Korea-Gallery` (public; Pages on the free plan needs it) |
| UI references | **User-supplied** — picked from curated roundups (Cosmos, Savee, Awwwards Gallery, etc.); see Phase 1 |

---

## Architecture Overview

```
┌─────────────────────┐     scheduled cron      ┌──────────────────────────┐
│  GitHub Actions      │────────────────────────▶│  Python Scraper          │
│  (scrape.yml, daily) │                         │  Playwright + parsers     │
└─────────────────────┘                         └────────────┬─────────────┘
                                          download+optimize   │ upsert metadata
                                  ┌───────────────────────────┼───────────────┐
                                  ▼                           ▼
                        ┌──────────────────┐      ┌────────────────────────┐
                        │ Cloudflare R2     │      │ Supabase Postgres       │
                        │ (image binaries)  │      │ (articles, credits,     │
                        └────────┬──────────┘      │  images, reactions,     │
                                 │  public URLs    │  lists) + Auth + RLS    │
                                 │                 └───────────┬─────────────┘
                                 ▼                             ▼
                        ┌───────────────────────────────────────────────┐
                        │  React SPA on GitHub Pages                      │
                        │  Vite + TS + Tailwind + Motion                  │
                        │  gallery grid · lightbox · filters · auth ·     │
                        │  reactions · lists                              │
                        └───────────────────────────────────────────────┘
```

**Two key principles:**
1. **Supabase is the single source of truth for the data model** — TypeScript types are generated from the DB schema (`supabase gen types`), so the frontend stays typed regardless of scraper language.
2. **Pluggable scraper** — a `SiteAdapter` interface isolates GQ-specific logic; new sites = new adapter, no pipeline rewrite.

---

## Tech Stack

- **Frontend:** Vite + React + TypeScript, Tailwind CSS, **Motion (Framer Motion)** for animation, `@supabase/supabase-js` client. Deployed to GitHub Pages via Actions.
- **Backend data/auth:** Supabase (Postgres, Auth with magic-link/Google, Row-Level Security).
- **Image storage:** Cloudflare R2 (S3-compatible; uploaded via `boto3`), public bucket with unguessable keys.
- **Scraper:** Python — Playwright (render lazy-loaded pages), selectolax/BeautifulSoup (parse), httpx (download), Pillow (resize→WebP + thumbnail), `supabase-py` (DB), `boto3` (R2).
- **CI/automation:** GitHub Actions (`scrape.yml` cron + `workflow_dispatch`; `deploy-web.yml` for Pages).

---

## Repository Structure (monorepo)

```
GQ-Korea-Gallery/
├── web/                       # React frontend (→ GitHub Pages)
│   ├── src/
│   │   ├── components/        # GalleryGrid, ImageCard, Lightbox, FilterBar, ListSidebar, AuthGate
│   │   ├── lib/              # supabaseClient, queries, generated types
│   │   ├── hooks/           # useReactions, useLists, useGalleryQuery
│   │   ├── motion/          # shared variants + easings (fade-up, stagger, shared-layout)
│   │   └── routes/
├── scraper/                   # Python pipeline
│   ├── gallery_scraper/
│   │   ├── core/            # SiteAdapter interface, fetch, parse helpers
│   │   ├── sites/gq_korea.py# first adapter
│   │   ├── images.py        # download + optimize
│   │   ├── storage_r2.py    # boto3 uploads
│   │   ├── db.py            # supabase-py upserts
│   │   └── pipeline.py      # discover → scrape → store
│   ├── tests/               # parser unit tests w/ saved HTML fixtures
│   └── pyproject.toml
├── supabase/migrations/       # SQL schema + RLS policies
└── .github/workflows/         # scrape.yml, deploy-web.yml
```

---

## Data Model (Supabase Postgres)

```sql
create type article_category as enum ('grooming','item','news','pictorial','sneakers');
create type reaction_type as enum ('like','dislike');

create table articles (
  id uuid primary key default gen_random_uuid(),
  source_site text not null default 'gq_korea',
  source_url  text not null unique,
  category    article_category not null,   -- from breadcrumb (홈 > STYLE > pictorial)
  title       text not null,
  published_date date,
  author_name text,
  author_url  text,
  content_hash text,                        -- detect edits on re-scrape
  scraped_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create table article_credits (              -- one row per role/person
  id uuid primary key default gen_random_uuid(),
  article_id uuid not null references articles(id) on delete cascade,
  role_raw   text not null,                 -- '포토그래퍼'
  role       text,                          -- normalized 'photographer'
  person_name text not null,                -- '장기평'
  agency     text                           -- '에스팀' (the "at ___" part), nullable
);

create table images (
  id uuid primary key default gen_random_uuid(),
  article_id uuid not null references articles(id) on delete cascade,
  r2_key     text not null,
  public_url text not null,
  thumb_url  text,
  width int, height int,
  position   int,                           -- order within article
  source_image_url text,
  content_hash text,
  created_at timestamptz not null default now(),
  unique (article_id, content_hash)         -- idempotent re-scrape
);

create table profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  created_at timestamptz not null default now()
);

create table reactions (                    -- per-user like/dislike
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  image_id uuid not null references images(id) on delete cascade,
  type reaction_type not null,
  created_at timestamptz not null default now(),
  unique (user_id, image_id)
);

create table lists (                        -- Pinterest-style boards
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now()
);

create table list_images (
  list_id  uuid not null references lists(id) on delete cascade,
  image_id uuid not null references images(id) on delete cascade,
  added_at timestamptz not null default now(),
  primary key (list_id, image_id)
);
```

**Tags model:** Intrinsic facts (category, site, author, credits) live on `articles`/`article_credits`. The "like/dislike tag" and "list-name tag" the user wants are modeled as **relations** (`reactions`, `list_images`) so each friend has their own — but the UI **renders them as tag chips** on each image, matching the mental model.

**RLS (mandatory — anon key ships in the public client):**
- `articles`, `images`, `article_credits`, `profiles`: read = `authenticated` only (private gallery). Writes only via service-role (scraper).
- `reactions`, `lists`, `list_images`: full CRUD restricted to owner (`user_id = auth.uid()`).

**Invite-only auth:** Disable public sign-ups in Supabase Auth; invite the handful of friends by email (magic link). Optional `allowed_emails` table + trigger if self-serve invites are wanted later.

---

## Scraper Design

**Pluggable interface** (`core`): `discover_article_urls(category) -> list[url]`, `parse_article(page) -> ArticleData`. `sites/gq_korea.py` is the first implementation.

**Per-run flow (GQ Korea):**
1. For each Style subcategory listing `/style/{grooming,item,news,pictorial,sneakers}/`, crawl pages.
   - **Initial backfill:** paginate to a configurable depth/cap.
   - **Incremental (scheduled):** newest-first; **stop at the first already-seen `source_url`** (listings are reverse-chronological) → cheap updates.
2. Per new article (Playwright render to defeat lazy-load):
   - **Category** ← breadcrumb segment (`홈 > STYLE > {category}`) — reliable, avoids "MORE LIKE THIS / MOST POPULAR" category confusion.
   - **Title / date / author(+url)** ← header block.
   - **Body images** ← scoped to the article body container only (exclude recommendation modules); resolve real URLs from `data-src`/`srcset`/rendered `<img>`.
   - **Credits** ← bottom block parsed into `{role_raw, role, person_name, agency}`, handling the `name at agency` form (e.g., `홍태준 at 에스팀`).
3. Download images → optimize (resize ≤1600px, WebP + small thumb) → upload to R2 → upsert `articles`/`article_credits`/`images`.

**Robustness/politeness:** rate-limit + concurrency cap, custom UA, respect `robots.txt`, retry/backoff, UTF-8 throughout, dedupe by `source_url` + image `content_hash`. Log parse-failure counts (alert if a run finds 0 new or error rate spikes → likely markup change).

---

## Frontend Design

### Design direction (distilled from chosen references)
- **Cosmos + Savee → the core gallery:** dense **masonry** of varied-aspect-ratio images, minimal/consistent gutters, image-first with quiet chrome; images **fade/scale in** as they load; infinite scroll. Hover reveals lightweight affordances (like/save). Click → focused detail. This is the "layout of the photo" the user liked; we refine it to feel clean/editorial (controlled column widths, generous page margins) rather than chaotic.
- **The FWA → a Timeline view:** a second browse mode that **clusters images by article date** (year → month → day) with sticky, animated date markers as you scroll — a natural fit since GQ articles carry timestamps. Toggle between **Grid ⇄ Timeline**.
- **Editorial vibe:** strong typographic hierarchy (GQ-like headlines), generous whitespace, content-first minimal UI. Responsive phone → desktop.

### Motion (defining feature) — Motion/Framer Motion
- Staggered **fade + slide-up reveals** as items scroll into view (`whileInView` / IntersectionObserver).
- **Gradual text appearance** for titles/metadata (sequenced opacity + translateY).
- Image **load-in** (gentle opacity/scale, blur-up from thumbnail).
- **Shared-element transition** grid thumb → detail (`layoutId`).
- Sticky **animated date headers** in Timeline; smooth filter/view transitions.
- **Respect `prefers-reduced-motion`.** Build with `frontend-design`, `ecc:motion-ui`, `ecc:motion-patterns`.

### Views & controls
- **Grid (masonry)** and **Timeline (by date)** modes.
- **Filters:** category (5 types), author, credit-person (photographer/model/…), date range, liked-by-me, in-list; **search** by title; sort newest/oldest.

### Image detail (lightbox)
Full image + attributes — title, category chip, date, author, **full credits** (role → name @ agency), source-article link, position. Actions: like / dislike, add-to-list (multi-select), open source.

### Auth & lists
Supabase magic-link/Google gate; per-user reactions & lists. **Lists view** = boards; each image addable to multiple lists.

---

## Scheduling & Update Detection

- `scrape.yml`: `schedule:` cron (e.g., daily) + `workflow_dispatch` (manual). Incremental logic above keeps runs cheap.
- The daily run **doubles as a Supabase keep-alive** (free projects pause after 1 week idle).
- Secrets in Actions: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, R2 (`account_id`, `access_key`, `secret`, `bucket`, `public_base_url`).
- `deploy-web.yml`: build `web/` → deploy to GitHub Pages on push to main.

---

## Phased Implementation Plan

Maps the user's 4 steps → concrete phases.

**Phase 0 — Setup (accounts + scaffold)**
Create GitHub repo **`zen-wang/GQ-Korea-Gallery`** (monorepo), Supabase project, R2 bucket; scaffold `web/` (Vite+TS+Tailwind+Motion) and `scraper/` (Python); wire Actions secrets.

**Phase 1 — UI reference research + design direction** *(user step 1)* — ✅ references chosen
**Chosen references:** [Cosmos](https://www.cosmos.so/) (photo layout) · [Savee](https://savee.com/) (photo layout) · [The FWA](https://thefwa.com/awards/) (timeline clustering) · [mymind](https://mymind.com/) (save-anything-to-notes → future). Design direction distilled into the Frontend Design section above. **Live screenshot capture** (desktop + mobile of each reference) happens at build start via the browser bridge (or user pastes screenshots) — used to lock spacing, type scale, and motion timings into design tokens before coding the grid/lightbox.

**Phase 2 — Schema + infra** *(user step 2)*
Write Supabase migrations (tables above) + RLS policies; configure R2 bucket/public access; generate TS types; set up invite-only auth. (Most design captured in this doc.)

**Phase 3 — Scraper (TDD)** *(user step 3a)*
Build GQ adapter parsers test-first against saved HTML fixtures (credits, breadcrumb category, date/author, lazy image URLs). Then pipeline: discover → render → parse → download/optimize → R2 → Supabase. Validate on the pictorial example, then all 5 categories; backfill; add incremental + `scrape.yml` cron.

**Phase 4 — Frontend (TDD where valuable)** *(user step 3b)*
Gallery grid + motion reveals; lightbox + attributes; filters/search; auth gate; reactions; lists; wire to Supabase + R2; `deploy-web.yml` → GitHub Pages.

**Phase 5 — Review & QA** *(user step 4)*
`code-reviewer` + `security-reviewer` (RLS, no service key in client, scraper correctness); Playwright E2E of core flows; perf (image lazy-load, query pagination, R2 CDN); fix issues.

---

## Testing Strategy

- **Scraper unit (pytest, highest value):** credits parser (`name at agency`, missing/extra roles), breadcrumb category extractor, date/author parse, lazy-image URL extraction — all against real saved HTML fixtures. Target 80%+ on parser/business logic.
- **DB integration:** upsert idempotency (dedupe by `source_url` + image `content_hash`); RLS policy tests.
- **Frontend:** Vitest + Testing Library for `ImageCard`/`Lightbox`/`FilterBar`; Playwright E2E for sign-in → browse → like → add-to-list → filter.

---

## Risks & Considerations

1. **Copyright/legal:** GQ imagery is Condé Nast's. This stays a **private, invite-only, personal-use** gallery (not public/commercial); we store source links + credits (attribution), respect `robots.txt`, and rate-limit politely. Keep access restricted.
2. **Lazy-loaded images:** confirmed base64 placeholders → use Playwright; validate the real CDN domain/URL pattern in Phase 3.
3. **Markup changes break parsers:** adapter pattern + fixture tests + run-health logging/alerting.
4. **Supabase auto-pause (1wk idle):** daily scraper keeps it warm.
5. **Free-tier limits:** optimize images (WebP, ≤1600px) to live within R2's 10GB; 500MB DB is ample for metadata; monitor.
6. **Anti-bot blocking:** polite cadence, UA, backoff; slow down if throttled.
7. **R2 public URLs:** images served from a public bucket with unguessable keys (gallery itself is auth-gated). Signed URLs are a future hardening option if stricter privacy is needed.

---

## Verification (end-to-end)

1. **Scraper (single article):** run pipeline on the pictorial example → assert 1 `articles` row with `category='pictorial'`, 6 `article_credits` rows (photographer 장기평 … model 홍태준 + agency 에스팀 …), ~12 `images` rows, and matching files in R2.
2. **Incremental:** re-run → confirm no duplicates (already-seen skipped).
3. **Workflow:** trigger `scrape.yml` via `workflow_dispatch` → completes, inserts rows.
4. **Frontend (local `npm run dev`):** sign in via magic link → grid renders with fade-up motion → click image → lightbox shows full attributes → like/dislike toggles → create list + add image (shows as chip) → filters (category/author/credit) + title search work.
5. **E2E:** Playwright covers sign-in → browse → like → add-to-list → filter.
6. **Deploy:** push → `deploy-web.yml` publishes to GitHub Pages → load live URL, sign in, verify.

---

## Out of Scope for v1 (future)

- **Notes / "save anything" (mymind-inspired):** save arbitrary content (not just scraped GQ images) into personal notes/clippings, attach annotations, optional AI auto-tagging. *(User-requested for later, explicitly not v1.)*
- Additional site adapters (other fashion sites), public/multi-tenant mode, recommendation/discovery, advanced edit-detection (content-hash diffing beyond new-article detection), signed image URLs.
