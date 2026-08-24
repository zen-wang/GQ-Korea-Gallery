-- GQ Korea Gallery — content, identity and curation schema.
--
-- Spec: PLAN.md §Data Model. Every deviation from that spec is called out
-- inline with a "PLAN.md deviation" comment and a reason.
--
-- Every table is locked down in the same statement group that creates it:
-- `enable row level security` immediately after each `create table`, plus a
-- blanket revoke. This is not stylistic. A Supabase project ships
--
--   alter default privileges for role postgres in schema public
--     grant all on tables to postgres, anon, authenticated, service_role;
--
-- and migrations run as postgres, so every `create table` hands `anon` — the
-- role the published anon key maps to — full read/write on a table whose RLS
-- is still off. If this file committed and the policy file did not follow
-- (interrupted push, two-paste dashboard flow, a failure in between), the
-- entire private gallery would be readable and writable by anyone holding the
-- key out of the deployed JS bundle. RLS enabled with no policy is deny-all,
-- so closing each table here costs nothing and removes the window entirely.
--
-- The next migration grants back exactly what each role needs and adds the
-- policies. If it never runs, the gallery is broken — never exposed.
--
-- gen_random_uuid() is core Postgres (13+), so no pgcrypto extension needed.

-- ---------------------------------------------------------------------------
-- Enumerations
-- ---------------------------------------------------------------------------

-- GQ Korea's Style tab. A future site with different sections needs
-- `alter type public.article_category add value '...'` in its own migration.
create type public.article_category as enum (
  'grooming', 'item', 'news', 'pictorial', 'sneakers'
);

create type public.reaction_type as enum ('like', 'dislike');

-- ---------------------------------------------------------------------------
-- Content — written only by the scraper (service role). Users only read.
-- ---------------------------------------------------------------------------

create table public.articles (
  id             uuid primary key default gen_random_uuid(),
  source_site    text not null default 'gq_korea',
  source_url     text not null unique,
  category       public.article_category not null,
  title          text not null,
  published_date date,
  author_name    text,
  author_url     text,
  -- Hash of the parsed body, so a re-scrape can spot an edited article
  -- without diffing every field. Nullable: older rows may predate hashing.
  content_hash   text,
  scraped_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
alter table public.articles enable row level security;
revoke all on table public.articles from anon, authenticated, service_role;

comment on table public.articles is
  'One row per scraped source article. Upsert key: source_url.';

-- Both grid modes order newest-first; the category chips filter on top of that.
create index articles_published_date_idx
  on public.articles (published_date desc nulls last, id);
create index articles_category_published_date_idx
  on public.articles (category, published_date desc nulls last, id);
-- Author filter (v1.1, PLAN.md §Views & controls).
create index articles_author_name_idx
  on public.articles (author_name)
  where author_name is not null;

create table public.article_credits (
  id          uuid primary key default gen_random_uuid(),
  article_id  uuid not null references public.articles(id) on delete cascade,
  -- PLAN.md deviation: added. The lightbox renders credits as an ordered list
  -- in source order; without this column a plain select returns them in
  -- whatever order Postgres feels like, which visibly reshuffles between loads.
  position    int  not null,
  role_raw    text not null,   -- as printed on the page, e.g. '포토그래퍼'
  role        text,            -- normalized, e.g. 'photographer'
  person_name text not null,   -- e.g. '서윤재'
  agency      text,            -- the 'at ___' suffix, e.g. '라온엠'
  -- Safe to make unique here, unlike on images below, because a re-scrape
  -- replaces an article's credits wholesale (delete then insert inside one
  -- transaction) rather than upserting them row by row.
  -- Doubles as the article_id index the cascade delete needs.
  unique (article_id, position)
);
alter table public.article_credits enable row level security;
revoke all on table public.article_credits from anon, authenticated, service_role;

comment on table public.article_credits is
  'One row per credited person. Re-scrape replaces an article''s credits wholesale.';

-- Credit-person filter (v1.1): "everything shot by this photographer".
create index article_credits_role_person_idx
  on public.article_credits (role, person_name);

create table public.images (
  id               uuid primary key default gen_random_uuid(),
  article_id       uuid not null references public.articles(id) on delete cascade,
  r2_key           text not null,
  public_url       text not null,
  -- PLAN.md deviation: not null. The grid renders thumbnails only
  -- (PHASE0_AMENDMENTS §B.4), so an image without one cannot be shown at all.
  thumb_url        text not null,
  -- PLAN.md deviation: not null. The masonry needs the aspect ratio to reserve
  -- space before the file loads, otherwise the column balance jumps. Pillow
  -- always reports both after a successful download.
  width            int  not null check (width  > 0),
  height           int  not null check (height > 0),
  position         int  not null,   -- order within the article body
  source_image_url text not null,
  -- PLAN.md deviation: not null. This is half the re-scrape dedupe key, and
  -- Postgres treats NULLs as distinct — leaving it nullable would let the
  -- unique constraint below pass on every single re-run, duplicating rows.
  content_hash     text not null,
  -- PLAN.md deviation: added, denormalised from articles.published_date and
  -- maintained by the two triggers at the bottom of this file (never by the
  -- pipeline, so it cannot drift). PHOTO mode — the default v1.0 view — orders
  -- every image by its *article's* date. With that key on the other table the
  -- planner has to scan and sort the whole images table for every page of
  -- infinite scroll. Denormalised, it is a plain index range scan, and the
  -- trailing id in images_feed_idx gives the feed a unique total order so
  -- keyset pagination can never duplicate or skip a tile.
  published_date   date,
  created_at       timestamptz not null default now(),
  -- The re-scrape dedupe key, and the ONLY unique constraint on this table.
  -- A second one would be unusable: `insert ... on conflict` can arbitrate on
  -- exactly one constraint, so any other unique index would raise instead of
  -- merging and abort the whole batch. That is also why position is indexed
  -- but not unique here.
  --
  -- Phase 3 pipeline requirement: collapse duplicates in memory before
  -- sending a batch. An editorial body that repeats a byte-identical image
  -- produces two rows with the same key, and a single INSERT carrying both
  -- fails with cardinality_violation no matter what this constraint says.
  unique (article_id, content_hash)
);
alter table public.images enable row level security;
revoke all on table public.images from anon, authenticated, service_role;

comment on table public.images is
  'One row per body image, re-hosted on R2. Dedupe key: (article_id, content_hash).';

create index images_article_position_idx
  on public.images (article_id, position);
-- Not unique: object keys may legitimately be shared when the same bytes
-- appear in more than one article, which is how a 10GB R2 budget survives.
create index images_r2_key_idx on public.images (r2_key);
-- The PHOTO-mode feed, in one index scan.
create index images_feed_idx
  on public.images (published_date desc nulls last, article_id, position, id);

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------

create table public.profiles (
  id           uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  created_at   timestamptz not null default now()
);
alter table public.profiles enable row level security;
revoke all on table public.profiles from anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- Curation — per user. Rendered as tag chips in the UI (PLAN.md §Tags model).
-- ---------------------------------------------------------------------------

create table public.reactions (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  image_id   uuid not null references public.images(id) on delete cascade,
  type       public.reaction_type not null,
  created_at timestamptz not null default now(),
  -- One verdict per image per person: like/dislike replaces, never stacks.
  unique (user_id, image_id)
);
alter table public.reactions enable row level security;
revoke all on table public.reactions from anon, authenticated, service_role;

create index reactions_image_id_idx on public.reactions (image_id);
-- "Liked by me" filter (v1.1).
create index reactions_user_type_idx on public.reactions (user_id, type);

create table public.lists (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  name       text not null check (length(btrim(name)) > 0),
  created_at timestamptz not null default now()
);
alter table public.lists enable row level security;
revoke all on table public.lists from anon, authenticated, service_role;

comment on column public.lists.name is
  'Not unique per user on purpose — the prototype lets you name freely.';

create index lists_user_id_idx on public.lists (user_id);

create table public.list_images (
  list_id  uuid not null references public.lists(id) on delete cascade,
  image_id uuid not null references public.images(id) on delete cascade,
  added_at timestamptz not null default now(),
  primary key (list_id, image_id)
);
alter table public.list_images enable row level security;
revoke all on table public.list_images from anon, authenticated, service_role;

-- The PK covers list_id; the cascade from images and the lightbox's
-- "which lists hold this image" lookup both need the other direction.
create index list_images_image_id_idx on public.list_images (image_id);

-- ---------------------------------------------------------------------------
-- Triggers
--
-- search_path is pinned to '' on every function so a caller cannot shadow the
-- unqualified names they resolve (Supabase linter: function_search_path_mutable).
-- pg_catalog is always searched, so built-ins still resolve.
-- ---------------------------------------------------------------------------

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger articles_set_updated_at
  before update on public.articles
  for each row execute function public.set_updated_at();

-- Keep images.published_date in step with its article, in both directions.
create or replace function public.images_inherit_published_date()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  select a.published_date into new.published_date
    from public.articles a
   where a.id = new.article_id;
  return new;
end;
$$;

create trigger images_inherit_published_date
  before insert or update of article_id on public.images
  for each row execute function public.images_inherit_published_date();

create or replace function public.articles_propagate_published_date()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  update public.images
     set published_date = new.published_date
   where article_id = new.id;
  return null;
end;
$$;

create trigger articles_propagate_published_date
  after update of published_date on public.articles
  for each row
  when (old.published_date is distinct from new.published_date)
  execute function public.articles_propagate_published_date();

-- Invite-only signup still goes through auth.users, so this covers invites too.
-- security definer is load-bearing: the role the auth service inserts the user
-- as (supabase_auth_admin) has no privileges on public.profiles at all, so
-- without it every invite fails at the trigger and no account can be created.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, display_name)
  values (
    new.id,
    coalesce(
      nullif(btrim(new.raw_user_meta_data ->> 'full_name'), ''),
      split_part(new.email, '@', 1)
    )
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
