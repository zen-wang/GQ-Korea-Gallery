# design-handoff — visual source of truth (reference, not build input)

Imported 2026-08-22 from the Claude Design project "GQ Korea Gallery Design"
(`fcdf4a76-7867-4c7a-a0fe-14652b0493e9`), read through the DesignSync API.

- `Index Prototype.dc.html` — the winning prototype. Open it in a browser
  (it loads `support.js` from this folder) to see the design running.
- `support.js` — the dc-runtime the prototype needs in order to render.
  Framework code, no design content; don't read it for design intent.

This folder is **committed but never imported into the build** — nothing under
`web/` may import from it. Per PHASE0_AMENDMENTS.md §D it is reference only:
tokens were extracted by hand into `web/src/index.css` (Tailwind v4 `@theme`)
and `web/src/motion/tokens.ts`, and screens get re-implemented in `web/`
against real Supabase data.

## Deviations — the prototype is not the scope

Read these before building anything from the prototype (PHASE0_AMENDMENTS.md §G):

| The prototype shows | Decision |
|---|---|
| ↓ download button in the lightbox | **Cut.** Private-viewing posture (§E) — do not build it. |
| POST / PHOTO mode toggle | **Keep** — ships in v1.0. |
| picsum placeholder images, `localStorage` lists | Stand-ins; real data is Supabase + R2. |
| `px` type sizes | Re-expressed as `rem` in `web/src/index.css` (§G.4). |

Not imported: `Gallery Directions.dc.html` (earlier direction exploration —
fetch it from the Design project if it's ever needed).
