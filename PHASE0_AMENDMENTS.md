# Phase 0 Amendments — read together with PLAN.md

Status: **Phase 0 complete.** PLAN.md remains the master spec; this doc records the decisions finalized on 2026-08-22 that amend it. Where they conflict, this doc wins.

## A. Platform: deployed web app, consumed on phones → PWA

- Primary user: one friend, browsing on **mobile Chrome / Safari**. No native app.
- Ship the React SPA as a **PWA**: `manifest.json` (name, icons, `display: standalone`, theme color), iOS meta tags (`apple-mobile-web-app-capable`, apple-touch-icon), so "Add to Home Screen" gives an app-like experience.
- GitHub Pages + SPA routing: use a **hash router** (simplest) or the 404.html redirect trick. Decide in Phase 2 scaffold; hash router is fine for a private tool.
- Service worker: optional; if added, cache app shell only — do NOT cache R2 images aggressively (storage bloat on the friend's phone).

## B. Mobile-first overrides to Frontend Design (PLAN.md §Frontend Design)

1. **No hover-dependent affordances.** PLAN.md's "hover reveals like/save" is desktop thinking. Grid tiles have exactly one interaction: tap → lightbox. Actions live inside the lightbox.
2. Masonry columns: **2 (phone) / 3 (tablet) / 4 (desktop)**.
3. Lightbox must support **swipe left/right** between images of the same article, **swipe down to dismiss**, pinch-zoom. Position indicator (n/total).
4. Bandwidth discipline: grid renders `thumb_url` only; `public_url` loads on lightbox open. `loading="lazy"` + IntersectionObserver as planned.
5. Tap targets ≥ 44px; filter chips horizontally scrollable on phone.

## C. Scope re-ordering: ship browsing first

Rationale: the friend's actual need is "look at photos and learn." Get real usage before building curation features. Schema is unchanged — reactions/lists tables ship in the Phase 2 migration as designed, the UI just comes later.

- **v1.0 (Phase 4a → deploy):** auth gate, masonry grid, category filters, lightbox with full credits + source link. PWA manifest.
- **v1.1 (Phase 4b):** reactions (like/dislike), lists/boards, filter-by-liked / in-list, author & credit-person filters.
- **v1.2 (Phase 4c):** Timeline view (date-clustered, sticky animated headers), search, sort.

PLAN.md Phases 0–3 and 5 are unchanged. Phase 5 (review/QA) runs before the v1.0 deploy and again after v1.1.

## D. Claude Design inserted into the workflow

Between Phase 1 (references — done) and Phase 4:

1. Paste `DESIGN_BRIEF.md` into a new Claude Design project → get 3 directions (phone viewport first).
2. Pick one, refine on canvas (spacing, type scale, motion timing).
3. `/design-sync` from Claude Code to pull the design system + components into `web/`.
4. Phase 4a implements against real Supabase data, keeping Design's tokens/components as the base. Motion work continues in code (Framer Motion) per PLAN.md.

Design output is a starting point, not production code: state management, Supabase wiring, auth, routing, and tests are all Claude Code work.

## E. Image hosting decision — confirmed with caveat

R2 re-hosting (as in PLAN.md) is confirmed over hotlinking: GQ's CDN will likely referer-block, and source images rot. This is acceptable **only while the gallery stays private and invite-only** (RLS-gated reads, no public sign-up, unguessable R2 keys). Do not convert this project to public access with re-hosted images; a public/portfolio version must switch to user-uploaded content.

## F. Scraper scope — unchanged, one clarification

Target = GQ Korea `/style` listings, 5 categories (grooming, item, news, pictorial, sneakers). Per article: body images + the **credits block** (role_raw / normalized role / person_name / agency, handling the `이름 at 에이전시` pattern). This matches PLAN.md §Scraper Design; no changes. Politeness rules (robots.txt, rate limit, UA, backoff) are mandatory, not optional.
