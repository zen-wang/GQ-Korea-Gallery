# Setup — the console steps

Phase 2 delivers the schema, the policies and the tests. Everything below has
to happen in a browser or a terminal you own, because it needs accounts and
secrets. Do it in this order; each step names what it unblocks.

Nothing here is required to run `supabase/tests/run_tests.sh`, which spins up
its own throwaway Postgres and needs no accounts at all.

---

## 1. Supabase project

1. Create a project at <https://supabase.com/dashboard>. Region: pick the one
   closest to Korea (`ap-northeast-2`) — the scraper writes from GitHub Actions
   but the friend reads from a phone in Korea, and reads are what feel slow.
2. From **Project Settings → API**, copy:
   - **Project URL** → `SUPABASE_URL`
   - **publishable** key (`sb_publishable_…`) → `SUPABASE_PUBLISHABLE_KEY`
   - **service_role** key → `SUPABASE_SERVICE_ROLE_KEY`

The publishable key is *meant* to be public — it ships inside the deployed JS
bundle. Prefer it over the legacy `anon` JWT: it rotates independently, whereas
rotating the legacy key means rotating the JWT secret and signing everyone out.
The service_role key is not: it bypasses RLS entirely. It belongs only in
GitHub Actions secrets and never in `web/`.

## 2. Apply the migrations

Either paste both files into the dashboard's SQL editor **in filename order**,
or use the CLI:

```bash
npx supabase link --project-ref <your-project-ref>
npx supabase db push
```

The first file creates every table already locked — RLS on, all privileges
revoked — so if the second never runs, the gallery is broken rather than
exposed. The second grants each role what it needs and adds the policies.
Order still matters, but a half-applied state is no longer dangerous.

Verify afterwards, in the SQL editor:

```sql
select tablename, rowsecurity from pg_tables
 where schemaname = 'public' order by tablename;

select grantee, table_name, privilege_type
  from information_schema.role_table_grants
 where table_schema = 'public' and grantee = 'anon';
```

Every table must show `rowsecurity = true`, and the second query must return
**zero rows** — `anon` is the role the published key maps to.

## 3. Invite-only auth

1. **Authentication → Sign In / Providers → Email**: keep it enabled, and turn
   **"Allow new users to sign up"** *off*. This is what makes the gallery
   invite-only; the RLS policies assume it.
2. **Authentication → URL Configuration**:
   - **Site URL** → the GitHub Pages URL (`https://zen-wang.github.io/GQ-Korea-Gallery/`).
   - **Redirect URLs** → add the same URL, and `http://localhost:5173/` for
     local development.

   Magic links fail with a confusing "requested path is invalid" error if the
   redirect URL is not on this list. If the app ends up using a hash router,
   add the `/#/` form too.
3. **Authentication → Users → Invite user** for each person, including
   yourself. The `handle_new_user` trigger creates their `profiles` row.

## 4. Image storage — nothing to do

Images live in Supabase Storage, in a bucket called `gallery` that migration
`20260823222834_supabase_storage.sql` creates for you. There is no second
vendor, no API token, and no card.

That last point is why: Cloudflare R2 requires a payment method on file to
activate even inside its free tier. Supabase Storage does not, and this project
already has a Supabase project.

What you trade is headroom — **1 GB** on the free plan instead of R2's 10 GB.
At PLAN.md's settings (WebP, long edge <= 1600px, plus a thumbnail) that is
roughly **3,000-3,500 images**, about 400 articles. Levers if it gets tight:
drop the long edge to 1200px and thumbs to 600px, which roughly doubles it, or
cap backfill depth per category. Egress is not the binding constraint — the
grid loads thumbnails only (PHASE0_AMENDMENTS §B.4), so a heavy session is
~25 MB against a 5 GB monthly allowance.

The bucket is **public with unguessable paths** — the same posture
PHASE0_AMENDMENTS §E chose for R2. `<img src>` works directly and the browser
caches by URL, which matters on a phone; signed URLs are re-minted per session
and would defeat that. Paths are `<article_id>/<hash>.webp`, prefixed by a v4
UUID, and `storage.objects` has RLS enabled with no policies, so the bucket
cannot be listed or enumerated and only the scraper's service role can write.

To confirm it exists, in the SQL editor:

```sql
select id, public, file_size_limit, allowed_mime_types from storage.buckets;
```

## 5. GitHub repository secrets

**Prerequisite:** secrets live on a repo, so the code has to be pushed first.
The repo name is load-bearing — it decides the Pages URL, which must match the
Site URL configured in step 3.

```bash
git remote add origin https://github.com/zen-wang/GQ-Korea-Gallery.git
git push -u origin main
```

| Secret | Used by | Notes |
|---|---|---|
| `SUPABASE_URL` | both workflows | already set |
| `SUPABASE_PUBLISHABLE_KEY` | `deploy-web.yml` | already set; public by design |
| `SUPABASE_SERVICE_ROLE_KEY` | `scrape.yml` | **the only one still needed** |

Set it with `gh` rather than the web UI — it prompts without echoing, so the
value never reaches your shell history or the screen:

```bash
gh secret set SUPABASE_SERVICE_ROLE_KEY   # paste, press enter
```

Find it at **Project Settings → API → service_role**. It bypasses RLS
entirely, so it belongs only here — never in `web/`, never in a commit.

`gh secret list` afterwards shows names and timestamps only; values are never
readable again, from the CLI or the UI.

Then **Settings → Pages → Source: GitHub Actions**. Pages from a *private* repo
requires GitHub Pro; on the free plan the repo has to be public. That is safe
here — the gallery's privacy comes from the auth gate and RLS, never from repo
visibility, and the publishable key is public by design. What must never be
committed is the `service_role` key.

## 6. Local development

```bash
cd web
cp .env.example .env.local     # fill in VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY
npm install && npm run dev
```

Only the publishable key goes in `web/.env.local`. `.env.local` is gitignored; keep it
that way.

Once the project exists, regenerate the types instead of hand-editing them:

```bash
SUPABASE_PROJECT_ID=<your-project-ref> npm run gen:types
```

---

## Running the database tests

```bash
brew install postgresql@14                       # any local postgres works
PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH" \
  bash supabase/tests/run_tests.sh
```

It creates a temporary cluster, applies both migrations, asserts the isolation
properties, and deletes the cluster. It never touches your Supabase project.
