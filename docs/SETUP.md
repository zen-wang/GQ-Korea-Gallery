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

## 4. Cloudflare R2 bucket

Cloudflare may ask for a payment method before it will enable R2, even though
the first 10 GB and all egress are free. Nothing here bills unless the bucket
grows past that.

1. **Create the bucket.** <https://dash.cloudflare.com> → **R2 object storage**
   → **Create bucket**. Name it `gq-gallery` → `R2_BUCKET`. Location: automatic
   is fine, or hint APAC.

2. **Turn on public access.** Open the bucket → **Settings** → under **Public
   Development URL** select **Enable**, then type `allow` to confirm. You get a
   base URL like `https://pub-<hash>.r2.dev` → `R2_PUBLIC_BASE_URL`.

   Cloudflare rate-limits `r2.dev` and labels it development-only. For two
   people browsing a gallery it is fine to start with, but a masonry grid fires
   dozens of thumbnail requests per scroll, so if images start failing to load,
   that is the cause. The upgrade is a custom domain (needs a domain on
   Cloudflare), which also brings caching and WAF.

   Switching later is cheap but not free: `images.public_url` and
   `images.thumb_url` store absolute URLs, so a base-URL change needs one
   `update public.images set public_url = replace(public_url, <old>, <new>)`
   (and the same for `thumb_url`) alongside the new secret.

3. **Create the API token.** On the R2 page, under **Account Details** → **API
   Tokens** → **Manage** → **Create API token**.
   - Permission: **Object Read & Write** — *not* Admin. Admin can create and
     delete buckets; the scraper only ever needs to put objects.
   - Scope it to the `gq-gallery` bucket only.
   - You get **Access Key ID** → `R2_ACCESS_KEY_ID` and **Secret Access Key** →
     `R2_SECRET_ACCESS_KEY`. **The secret is shown once.** Put it straight into
     the GitHub secret in step 5; if you lose it, delete the token and make a
     new one.

4. **Account ID** → `R2_ACCOUNT_ID`, shown in the R2 page sidebar under Account
   Details.

No CORS rules are needed: the gallery only ever puts R2 URLs in `<img src>`,
which is not a CORS request. That stays true as long as nothing calls `fetch()`
on an image — which is one more reason the lightbox download button was cut
(PHASE0_AMENDMENTS §G.2).

Object keys must be unguessable. The bucket is public and the gallery's privacy
at the *file* level rests on nobody being able to enumerate keys
(PHASE0_AMENDMENTS §E).

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
| `SUPABASE_URL` | both workflows | |
| `SUPABASE_PUBLISHABLE_KEY` | `deploy-web.yml` | baked into the public bundle, by design |
| `SUPABASE_SERVICE_ROLE_KEY` | `scrape.yml` | bypasses RLS — scraper only |
| `R2_ACCOUNT_ID` | `scrape.yml` | |
| `R2_ACCESS_KEY_ID` | `scrape.yml` | |
| `R2_SECRET_ACCESS_KEY` | `scrape.yml` | |
| `R2_BUCKET` | `scrape.yml` | |
| `R2_PUBLIC_BASE_URL` | `scrape.yml` | written into `images.public_url` |

Set them with `gh` rather than the web UI — it prompts without echoing, so no
secret ends up in your shell history or on screen:

```bash
gh secret set SUPABASE_SERVICE_ROLE_KEY   # paste, press enter
gh secret set R2_ACCOUNT_ID
gh secret set R2_ACCESS_KEY_ID
gh secret set R2_SECRET_ACCESS_KEY
gh secret set R2_BUCKET
gh secret set R2_PUBLIC_BASE_URL
```

`gh secret list` afterwards shows names and timestamps only — values are never
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
