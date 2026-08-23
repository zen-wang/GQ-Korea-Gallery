# GQ Korea Gallery

Private, invite-only fashion-editorial image gallery. Scrapes GQ Korea's Style
tab, re-hosts images on Cloudflare R2, stores metadata in Supabase, and serves
a mobile-first PWA gallery from GitHub Pages.

- **PLAN.md** — master spec (architecture, data model, phases)
- **PHASE0_AMENDMENTS.md** — locked amendments (mobile-first PWA, scope order, design handoff)

## Monorepo layout

| Path | What |
|---|---|
| `web/` | Vite + React + TypeScript + Tailwind CSS v4 + Motion frontend |
| `scraper/` | Python scraping pipeline (`gallery_scraper` package) |
| `supabase/` | Schema + RLS migrations, and throwaway-cluster tests for them |
| `docs/SETUP.md` | Console steps: Supabase project, invite-only auth, R2 bucket, Actions secrets |
| `.github/workflows/` | `scrape.yml` (cron scraper), `deploy-web.yml` (Pages deploy) |
| `design-handoff/` | Imported Claude Design prototype — visual source of truth, never imported into the build |

Design tokens extracted from the prototype live in `web/src/index.css`
(Tailwind `@theme`) and `web/src/motion/tokens.ts`.
