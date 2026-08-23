# Phase 0 Amendments — read together with PLAN.md

Status: **Phase 0 complete.** PLAN.md remains the master spec; this doc records the decisions finalized on 2026-08-22 that amend it. Where they conflict, this doc wins.

## A. Platform: deployed web app, consumed on phones → PWA

- Primary user: one friend, browsing on **mobile Chrome / Safari**. No native app.
- Ship the React SPA as a **PWA**: `manifest.json` (name, icons, `display: standalone`, theme color), iOS meta tags (`apple-mobile-web-app-capable`, apple-touch-icon), so "Add to Home Screen" gives an app-like experience.
- GitHub Pages + SPA routing: use a **hash router** (simplest) or the 404.html redirect trick. Decide in Phase 2 scaffold; hash router is fine for a private tool.
- Service worker: optional; if added, cache app shell only — do NOT cache gallery images aggressively (storage bloat on the friend's phone).

## B. Mobile-first overrides to Frontend Design (PLAN.md §Frontend Design)

1. **No hover-dependent affordances.** PLAN.md's "hover reveals like/save" is desktop thinking. Grid tiles have exactly one interaction: tap → lightbox. Actions live inside the lightbox.
2. Masonry columns: **2 (phone) / 3 (tablet) / 4 (desktop)**.
3. Lightbox must support **swipe left/right** between images of the same article, **swipe down to dismiss**, pinch-zoom. Position indicator (n/total).
4. Bandwidth discipline: grid renders `thumb_url` only; `public_url` loads on lightbox open. `loading="lazy"` + IntersectionObserver as planned.
5. Tap targets ≥ 44px; filter chips horizontally scrollable on phone.

## C. Scope re-ordering: ship browsing first

Rationale: the friend's actual need is "look at photos and learn." Get real usage before building curation features. Schema is unchanged — reactions/lists tables ship in the Phase 2 migration as designed, the UI just comes later.

- **v1.0 (Phase 4a → deploy):** auth gate, masonry grid, category filters, POST/PHOTO mode toggle, lightbox with full credits + source link. PWA manifest (icons are a Phase 4a exit requirement — §G.3).
- **v1.1 (Phase 4b):** reactions (like/dislike), lists/boards, filter-by-liked / in-list, author & credit-person filters.
- **v1.2 (Phase 4c):** Timeline view (date-clustered, sticky animated headers), search, sort.

PLAN.md Phases 0–3 and 5 are unchanged. Phase 5 (review/QA) runs before the v1.0 deploy and again after v1.1.

## D. Claude Design → Code handoff (corrected 2026-08-22)

/design-sync is repo → Design (up-sync: imports this codebase's design system
so Design composes with our real components). It does NOT pull finished
designs down. Actual handoff of the finished design:

1. Tokens: design-sync "pull theme tokens" option — winning direction's
   theme.json + styles.css land in web/ as the aesthetic base.
2. Full screens: from Claude Design, Export → "Handoff to Claude Code"
   (enable Settings → Claude product access if the option is missing).
   Fallback: ZIP export into design-handoff/, reference only.
3. Claude Code re-implements in web/ (Vite + TS + Tailwind + Motion) against
   real Supabase data. Design output = tokens + reference, never production code.
4. After v1.0 ships and web/ has real components, /design-sync up-sync becomes
   the right tool: v1.1 design work (reactions/lists UI) starts from OUR
   components instead of generic ones.

**What actually happened (2026-08-22):** the project's files were read straight
out of Claude Design through the DesignSync read API into `design-handoff/`,
which is **committed** — the prototype is the visual source of truth and it
belongs in the repo where reviewers can open it. Tokens were extracted by hand
into `web/src/index.css` (Tailwind v4 `@theme`) and `web/src/motion/tokens.ts`
instead of arriving as a theme.json. Steps 3 and 4 stand unchanged: nothing in
`web/` imports from `design-handoff/`.

## E. Image hosting decision — confirmed, vendor revised 2026-08-23

Re-hosting (as in PLAN.md) is confirmed over hotlinking: GQ's CDN will likely referer-block, and source images rot. This is acceptable **only while the gallery stays private and invite-only** (RLS-gated reads, no public sign-up, unguessable object paths). Do not convert this project to public access with re-hosted images; a public/portfolio version must switch to user-uploaded content.

**Vendor: Supabase Storage, not Cloudflare R2.** R2 requires a payment method
on file to activate even inside its free tier, and this project is meant to
cost nothing. Supabase Storage needs no card, is already part of the project,
and removes a vendor plus five CI secrets. The trade is a **1 GB** ceiling
instead of R2's 10 GB — roughly 3,000-3,500 images at PLAN.md's WebP/1600px
settings, about 400 articles. If that binds, the levers in order are: reduce
the long edge to 1200px, cap backfill depth per category, then move to another
S3-compatible host (Backblaze B2 is the obvious candidate). Because the schema
stores a bucket-relative `storage_path` rather than a vendor URL, moving again
is an uploader change plus one `UPDATE` over `public_url`/`thumb_url`.

The bucket is public with unguessable paths, which is the same posture this
section already accepted for R2: `<img src>` works directly and the browser
caches by URL, whereas signed URLs are re-minted per session and would defeat
caching on the phone-first grid (§B.4). Private-bucket-plus-signed-URLs remains
the hardening option if the privacy bar ever rises. `storage.objects` keeps
RLS enabled with no policies, so nothing but the scraper's service role writes,
and the bucket cannot be listed.

## F. Scraper scope — unchanged, one clarification

Target = GQ Korea `/style` listings, 5 categories (grooming, item, news, pictorial, sneakers). Per article: body images + the **credits block** (role_raw / normalized role / person_name / agency, handling the `이름 at 에이전시` pattern). This matches PLAN.md §Scraper Design; no changes. Politeness rules (robots.txt, rate limit, UA, backoff) are mandatory, not optional.

## G. Prototype-derived scope decisions (2026-08-22)

Settled after reviewing the imported prototype. The prototype is a design
artifact, not a scope document — where it and this section disagree, this
section wins.

1. **POST/PHOTO mode toggle → in v1.0.** The prototype's header toggle switches
   the grid between one tile per article (cover + title + image count) and one
   tile per image. It costs a single query filter and serves the core "flip
   through photos and learn" use directly, so it ships in v1.0 (§C) even though
   PLAN.md never specified it.

2. **Lightbox download button → cut.** The prototype's ↓ button saves the
   full-size image to the device. §E's re-hosting decision rests on this
   staying a *private viewing* tool; a one-tap save of re-hosted Condé Nast
   imagery pushes the posture toward redistribution, and the lightbox already
   carries a source link for anyone who wants the original. Do not implement
   it — not in v1.0, not later.

3. **PWA manifest icons → Phase 4a exit checklist.** `web/public/manifest.webmanifest`
   ships with an empty `icons` array. "Add to Home Screen" works without them,
   but the install prompt will not fire and the home-screen icon falls back to
   a screenshot. Generating the set (192px + 512px, plus `apple-touch-icon`) is
   a required item in Phase 4a's finishing checklist, before the v1.0 deploy.

4. **Type scale in `rem`, structure in `px`.** `--text-*` tokens are `rem` so
   the reader's system font-size setting scales the UI — a real need for a
   phone-only audience. Structural dimensions (gutter, hairlines, the 44px tap
   target, tab-bar and sheet-row heights, frame max-width) stay `px` so layout
   and touch targets hold steady while text scales.
