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

## Decisions worth knowing

- **`images.content_hash` is the only unique key on `images`.** `insert ... on
  conflict` can arbitrate exactly one constraint, so a second unique index
  would raise instead of merging and abort a whole scrape batch. That is why
  `r2_key` and `position` are indexed but not unique — and why the same
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

## Left for Phase 3

- The pipeline must **collapse duplicate images in memory before sending a
  batch**. A body that repeats a byte-identical image yields two rows with the
  same `(article_id, content_hash)`, and one INSERT carrying both fails with
  `cardinality_violation` regardless of the constraint.
- The pipeline should treat `images` as **insert-or-update, never delete**.
  `reactions.image_id` and `list_images.image_id` cascade, so deleting and
  re-inserting an image row during reconciliation would silently drop it out of
  everyone's saved lists.

Console steps (creating the project, invite-only auth, R2, secrets) are in
[../docs/SETUP.md](../docs/SETUP.md).
