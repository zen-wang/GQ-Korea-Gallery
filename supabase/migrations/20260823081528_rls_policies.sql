-- GQ Korea Gallery — privileges and Row-Level Security policies.
--
-- This is the file that makes the gallery usable. The previous migration
-- already closed every table (RLS on, all privileges revoked), so until this
-- one runs nobody can read anything. Here we hand each role back exactly what
-- it needs and no more.
--
-- The anon key ships inside the public JS bundle on GitHub Pages, so anyone
-- can read it and talk to PostgREST directly. Everything below assumes the
-- attacker already has it.
--
-- Three layers, deliberately redundant:
--   1. GRANTs decide which verbs a role may even attempt. Users get SELECT on
--      content and CRUD on their own curation — nothing more. This layer also
--      catches what RLS cannot: TRUNCATE ignores row-level policies entirely,
--      so a role holding Supabase's default ALL grant could empty a table
--      that no policy would ever let it read.
--   2. RLS decides which rows those verbs see. A table with RLS on and no
--      matching policy denies everything.
--   3. The service role (scraper only, never the browser) bypasses RLS by
--      design. That is why content tables need no write policies at all.
--
-- auth.uid() is wrapped as (select auth.uid()) throughout: that makes it an
-- InitPlan evaluated once per statement instead of once per row.

-- ---------------------------------------------------------------------------
-- Layer 1 — privileges
-- ---------------------------------------------------------------------------

-- Repeated from the schema migration so this file is correct on its own, and
-- so re-running it re-establishes the floor before granting anything back.
revoke all on table
  public.articles, public.article_credits, public.images,
  public.profiles, public.reactions, public.lists, public.list_images
  from anon, authenticated, service_role;

-- Supabase arms `alter default privileges ... grant all on tables to anon,
-- authenticated` at the project level, so without this every table a future
-- migration creates would arrive world-open again and depend on somebody
-- remembering to close it. Disarm it once; from here on, access to a new
-- table is whatever that table's own migration grants explicitly.
alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;

-- Content is read-only to every signed-in reader. No INSERT/UPDATE/DELETE
-- grant exists, so the scraper's service role is the only writer.
grant select on table
  public.articles, public.article_credits, public.images
  to authenticated;

-- Identity: read the roster, and edit only your own display name. The column
-- list matters — a table-wide UPDATE grant would also expose created_at.
grant select                on table public.profiles to authenticated;
grant update (display_name) on table public.profiles to authenticated;

-- Curation is the user's own data.
grant select, insert, update, delete on table
  public.reactions, public.lists, public.list_images
  to authenticated;

-- The scraper connects as service_role, which also bypasses RLS. Granted
-- explicitly so this migration does not silently depend on Supabase's default
-- privileges, and withheld from the curation tables so that the service key —
-- which lives in GitHub Actions secrets, the least-protected place we keep a
-- credential — cannot read or alter anyone's reactions and saved lists.
grant select, insert, update, delete on table
  public.articles, public.article_credits, public.images
  to service_role;

-- ---------------------------------------------------------------------------
-- Layer 2 — row-level policies
--
-- RLS itself was enabled in the schema migration, at create-table time.
-- ---------------------------------------------------------------------------

-- --- Content: any signed-in reader sees all of it; nobody writes it ---------

create policy "articles are readable by signed-in users"
  on public.articles for select to authenticated using (true);

create policy "credits are readable by signed-in users"
  on public.article_credits for select to authenticated using (true);

create policy "images are readable by signed-in users"
  on public.images for select to authenticated using (true);

-- --- Identity --------------------------------------------------------------

create policy "profiles are readable by signed-in users"
  on public.profiles for select to authenticated using (true);

create policy "a user updates only their own profile"
  on public.profiles for update to authenticated
  using      ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);

-- No INSERT policy: rows arrive through public.handle_new_user(), which is
-- security definer and therefore not subject to RLS. No DELETE policy either —
-- profiles die with the auth.users row via cascade.

-- --- Curation: strictly owner-scoped ---------------------------------------
--
-- `for all` with matching using + with check covers every verb correctly:
-- SELECT/DELETE test `using` on the existing row, INSERT tests `with check` on
-- the new row, UPDATE tests `using` on the old row and `with check` on the new
-- one — so a user can neither read, retarget, nor plant a row under another
-- person's id.

create policy "a user owns their reactions"
  on public.reactions for all to authenticated
  using      ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "a user owns their lists"
  on public.lists for all to authenticated
  using      ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

-- list_images carries no user_id, so ownership is derived from the parent
-- list. The subquery runs as the calling user, so RLS on public.lists applies
-- to it as well — a list you cannot see can never satisfy this predicate.
create policy "a user owns the contents of their lists"
  on public.list_images for all to authenticated
  using (
    exists (
      select 1 from public.lists l
      where l.id = list_images.list_id
        and l.user_id = (select auth.uid())
    )
  )
  with check (
    exists (
      select 1 from public.lists l
      where l.id = list_images.list_id
        and l.user_id = (select auth.uid())
    )
  );
