# supabase/

| Path | What |
|---|---|
| `migrations/` | Schema and RLS policies, applied in filename order |
| `tests/` | Throwaway-cluster tests for the constraints and the policies |

`20260822120000_initial_schema.sql` creates every table **already closed** —
`enable row level security` and a blanket revoke immediately after each
`create table`. `20260822120100_rls_policies.sql` then grants each role exactly
what it needs and adds the policies.

That split matters because of a Supabase detail: every project arms
`alter default privileges ... grant all on tables to anon, authenticated`, and
migrations run as `postgres`. So each `create table` silently hands `anon` —
the role the published anon key maps to — full read/write. Closing each table
in the same file removes any committed state where the gallery is reachable.
If the policy migration never runs, the gallery is broken, never exposed.

## Tests

No accounts and no network: the script builds its own Postgres, applies both
migrations and deletes the cluster.

```bash
PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH" bash supabase/tests/run_tests.sh
```

`tests/bootstrap.sql` reproduces Supabase's default privileges and the real
`auth.uid()` on purpose. A stub that is *more restrictive* than production is
worse than no stub at all — it makes a revoke that does nothing in production
look green on a laptop.

The suite is mutation-checked: removing the revokes, the `enable row level
security` statements, the `security definer` on `handle_new_user()`, or a
single policy each turn it red.

**Known fidelity gap:** the harness above runs whatever local Postgres is on
PATH — Postgres 14 as written — while the live project is Postgres 17.
Everything in these migrations is 14-compatible so it passes on both, but that
is the same class of gap that let the default-privileges bug through once
already: a test environment that is not the target. Closing it is
`brew install postgresql@17` and changing the PATH in the command above.

## Decisions worth knowing

- **`images.content_hash` is the only unique key on `images`.** `insert ... on
  conflict` can arbitrate exactly one constraint, so a second unique index
  would raise instead of merging and abort a whole scrape batch. That is why
  `storage_path` and `position` are indexed but not unique — and why the same
  `unique (article_id, position)` *is* safe on `article_credits`, which a
  re-scrape replaces wholesale rather than upserting.
- **`images.published_date` is denormalised** from `articles` and maintained by
  trigger, never by the pipeline. PHOTO mode sorts every image by its article's
  date; with that key on the other table, each page of infinite scroll scans
  and sorts the whole images table. The trailing `id` in `images_feed_idx`
  gives the feed a unique total order, so keyset pagination cannot duplicate or
  skip a tile.
- **`service_role` is withheld from the curation tables.** The scraper's key
  lives in GitHub Actions secrets and never needs anyone's reactions or lists.
- **Images live in the `gallery` Storage bucket**, created by migration
  `20260823222834`. Public with unguessable paths so `<img src>` caches; only
  service_role can write, because `storage.objects` has RLS on and no policies.

## What Phase 3 does with this schema

Both constraints below are honoured by `scraper/gallery_scraper/`, and each has
a test that fails if it stops being:

- Duplicate images are **collapsed in memory before the batch is sent**. A body
  that repeats a byte-identical image yields two rows with the same
  `(article_id, content_hash)`, and one INSERT carrying both fails with
  `cardinality_violation` regardless of the constraint.
- `images` is **insert-or-update, never delete**. `reactions.image_id` and
  `list_images.image_id` cascade, so deleting and re-inserting during
  reconciliation would silently drop an image out of everyone's saved lists.

One column gained a second job. **`articles.content_hash` is the pipeline's
completion marker**: written NULL on upsert, filled in only once every image of
that article has been stored. "Already have it" therefore means *content_hash is
not null*, not *a row exists* — which is what lets a capped, timed-out or
part-failed run be finished by the next one instead of being stranded behind it.
`scraper/README.md` explains why that distinction is load-bearing.

Note the gap between that and the column's original purpose: nothing yet
*compares* a stored hash against a freshly computed one, so an edited article is
currently skipped whatever its digest says. The digest is a faithful record of
which version was stored; wiring it up to actually detect edits is future work,
and the hash already covers every field it would need.

Console steps (creating the project, invite-only auth, Storage, secrets) are in
[../docs/SETUP.md](../docs/SETUP.md).
