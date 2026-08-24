-- RLS and constraint tests (PLAN.md §Testing Strategy: "RLS policy tests").
-- Run via ./run_tests.sh, which applies the migrations to a throwaway cluster
-- seeded by bootstrap.sql — including Supabase's default privileges, without
-- which every revoke under test here would be a no-op.
--
-- Any failure raises and aborts the run (psql -v ON_ERROR_STOP=1).
-- All data below is synthetic.

\set QUIET on
\set user_a '11111111-1111-1111-1111-111111111111'
\set user_b '22222222-2222-2222-2222-222222222222'
\set art    '33333333-3333-3333-3333-333333333333'
\set img1   '44444444-4444-4444-4444-444444444444'
\set img2   '55555555-5555-5555-5555-555555555555'
\set list_b '66666666-6666-6666-6666-666666666666'

-- ---------------------------------------------------------------------------
-- Seed
-- ---------------------------------------------------------------------------

-- Users arrive through the low-privilege auth role, exactly as an invite does.
-- This is what proves `security definer` on handle_new_user() is load-bearing:
-- drop it and these two inserts fail with permission denied for public.profiles.
begin;
set local role auth_admin;
insert into auth.users (id, email, raw_user_meta_data) values
  (:'user_a', 'a@example.test', '{"full_name":"Reader A"}'),
  (:'user_b', 'b@example.test', '{}');
commit;

-- Content arrives as superuser, standing in for the scraper's service role.
insert into public.articles (id, source_url, category, title, published_date, author_name)
values (:'art', 'https://www.gqkorea.co.kr/test-a1', 'pictorial', '블랙 앤 화이트',
        '2026-07-12', '김에디터');

insert into public.article_credits (article_id, position, role_raw, role, person_name, agency) values
  (:'art', 2, '모델',       'model',        '표도현', '라온엠'),
  (:'art', 1, '포토그래퍼', 'photographer', '서윤재', null);

insert into public.images
  (id, article_id, storage_path, public_url, thumb_url, width, height, position,
   source_image_url, content_hash)
values
  (:'img1', :'art', 'gq/a1/01.webp', 'https://cdn.test/1.webp', 'https://cdn.test/1t.webp',
   1600, 2133, 1, 'https://src.test/1.jpg', 'hash-one'),
  (:'img2', :'art', 'gq/a1/02.webp', 'https://cdn.test/2.webp', 'https://cdn.test/2t.webp',
   1600, 1067, 2, 'https://src.test/2.jpg', 'hash-two');

-- User B's private curation, for the isolation tests below.
insert into public.reactions (user_id, image_id, type) values (:'user_b', :'img1', 'like');
insert into public.lists (id, user_id, name) values (:'list_b', :'user_b', 'B private list');
insert into public.list_images (list_id, image_id) values (:'list_b', :'img1');

-- ---------------------------------------------------------------------------
-- A. The controls are actually armed
--
-- Everything after this section tests behaviour. This section tests that the
-- mechanisms producing that behaviour exist at all — without it, deleting
-- `enable row level security` from the schema leaves the whole suite green.
-- ---------------------------------------------------------------------------

do $$
declare t text; n int;
begin
  foreach t in array array['articles','article_credits','images','profiles',
                           'reactions','lists','list_images'] loop
    if not (select relrowsecurity from pg_class where oid = ('public.'||t)::regclass) then
      raise exception 'SECURITY FAIL: RLS is not enabled on public.%', t;
    end if;
    select count(*) into n from pg_policies
     where schemaname = 'public' and tablename = t;
    if n = 0 then
      raise exception 'SECURITY FAIL: public.% has RLS on but no policy at all', t;
    end if;
  end loop;
  raise notice 'ok  RLS enabled and policied on all seven tables';
end $$;

-- Supabase's default privileges hand every role ALL on each table at create
-- time. These assertions check the migrations took back exactly the right
-- things — the layer that RLS cannot substitute for, since TRUNCATE ignores
-- row policies entirely.
do $$
declare bad text;
begin
  select string_agg(distinct table_name || ':' || privilege_type, ', ') into bad
    from information_schema.role_table_grants
   where table_schema = 'public' and grantee = 'anon';
  if bad is not null then
    raise exception 'SECURITY FAIL: anon still holds %', bad;
  end if;

  select string_agg(distinct table_name || ':' || privilege_type, ', ') into bad
    from information_schema.role_table_grants
   where table_schema = 'public'
     and grantee = 'authenticated'
     and table_name in ('articles','article_credits','images')
     and privilege_type <> 'SELECT';
  if bad is not null then
    raise exception 'SECURITY FAIL: authenticated can write content: %', bad;
  end if;

  select string_agg(distinct table_name || ':' || privilege_type, ', ') into bad
    from information_schema.role_table_grants
   where table_schema = 'public'
     and grantee = 'authenticated'
     and table_name = 'profiles'
     and privilege_type <> 'SELECT';
  if bad is not null then
    raise exception 'SECURITY FAIL: authenticated holds table-wide % on profiles', bad;
  end if;

  select string_agg(distinct table_name, ', ') into bad
    from information_schema.role_table_grants
   where table_schema = 'public'
     and grantee in ('anon','authenticated')
     and privilege_type = 'TRUNCATE';
  if bad is not null then
    raise exception 'SECURITY FAIL: TRUNCATE granted on %, which ignores RLS', bad;
  end if;

  select string_agg(distinct table_name || ':' || privilege_type, ', ') into bad
    from information_schema.role_table_grants
   where table_schema = 'public'
     and grantee = 'service_role'
     and table_name in ('reactions','lists','list_images','profiles');
  if bad is not null then
    raise exception 'SECURITY FAIL: the scraper key holds %', bad;
  end if;

  -- Every public function is exposed at /rest/v1/rpc/<name> to whichever role
  -- can execute it, and Postgres grants EXECUTE to PUBLIC by default. None of
  -- ours are meant to be called that way. (Supabase advisor lints 0028/0029.)
  select string_agg(p.proname, ', ') into bad
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public'
     and (has_function_privilege('anon', p.oid, 'EXECUTE')
       or has_function_privilege('authenticated', p.oid, 'EXECUTE'));
  if bad is not null then
    raise exception 'SECURITY FAIL: anon/authenticated can call public.% over RPC', bad;
  end if;

  raise notice 'ok  default privileges revoked; every grant matches intent';
end $$;

-- ---------------------------------------------------------------------------
-- B. Schema behaviour
-- ---------------------------------------------------------------------------

do $$
declare nm text;
begin
  if (select count(*) from public.profiles) <> 2 then
    raise exception 'handle_new_user did not create both profiles';
  end if;

  select display_name into nm from public.profiles where id = '11111111-1111-1111-1111-111111111111';
  if nm <> 'Reader A' then raise exception 'full_name not used, got %', nm; end if;

  -- No full_name in metadata falls back to the email local part.
  select display_name into nm from public.profiles where id = '22222222-2222-2222-2222-222222222222';
  if nm <> 'b' then raise exception 'email fallback failed, got %', nm; end if;

  raise notice 'ok  profiles auto-create through the low-privilege auth role';
end $$;

do $$
declare before_ts timestamptz; after_ts timestamptz;
begin
  select updated_at into before_ts from public.articles
   where id = '33333333-3333-3333-3333-333333333333';
  update public.articles set title = title
   where id = '33333333-3333-3333-3333-333333333333';
  select updated_at into after_ts from public.articles
   where id = '33333333-3333-3333-3333-333333333333';
  if after_ts <= before_ts then raise exception 'set_updated_at did not bump updated_at'; end if;
  raise notice 'ok  articles.updated_at bumped on update';
end $$;

do $$
declare d date;
begin
  -- The denormalised feed key must be inherited on insert...
  select distinct published_date into d from public.images
   where article_id = '33333333-3333-3333-3333-333333333333';
  if d <> date '2026-07-12' then
    raise exception 'images.published_date not inherited on insert, got %', d;
  end if;

  -- ...and follow the article when it is corrected, or the feed silently
  -- sorts on a stale date.
  update public.articles set published_date = date '2026-07-20'
   where id = '33333333-3333-3333-3333-333333333333';
  select distinct published_date into d from public.images
   where article_id = '33333333-3333-3333-3333-333333333333';
  if d <> date '2026-07-20' then
    raise exception 'images.published_date did not follow the article, got %', d;
  end if;

  update public.articles set published_date = date '2026-07-12'
   where id = '33333333-3333-3333-3333-333333333333';
  raise notice 'ok  images.published_date tracks its article in both directions';
end $$;

do $$
begin
  -- This is the re-scrape idempotency guarantee (PLAN.md §Verification step 2).
  begin
    insert into public.images
      (article_id, storage_path, public_url, thumb_url, width, height, position,
       source_image_url, content_hash)
    values ('33333333-3333-3333-3333-333333333333', 'gq/a1/dup.webp',
            'https://cdn.test/d.webp', 'https://cdn.test/dt.webp', 800, 800, 3,
            'https://src.test/d.jpg', 'hash-one');
    raise exception 'FAIL: duplicate (article_id, content_hash) was accepted';
  exception when unique_violation then null;
  end;
  raise notice 'ok  duplicate image content_hash rejected';

  begin
    insert into public.images
      (article_id, storage_path, public_url, thumb_url, width, height, position,
       source_image_url, content_hash)
    values ('33333333-3333-3333-3333-333333333333', 'gq/a1/null.webp',
            'https://cdn.test/n.webp', 'https://cdn.test/nt.webp', 800, 800, 4,
            'https://src.test/n.jpg', null);
    raise exception 'FAIL: null content_hash was accepted — dedupe would never fire';
  exception when not_null_violation then null;
  end;
  raise notice 'ok  null image content_hash rejected';

  begin
    insert into public.lists (user_id, name)
    values ('11111111-1111-1111-1111-111111111111', '   ');
    raise exception 'FAIL: blank list name was accepted';
  exception when check_violation then null;
  end;
  raise notice 'ok  blank list name rejected';

  -- One verdict per image per person; a second one must collide, not stack.
  begin
    insert into public.reactions (user_id, image_id, type)
    values ('22222222-2222-2222-2222-222222222222',
            '44444444-4444-4444-4444-444444444444', 'dislike');
    raise exception 'FAIL: a second reaction on the same image was accepted';
  exception when unique_violation then null;
  end;
  raise notice 'ok  one reaction per image per user enforced';
end $$;

do $$
declare first_role text;
begin
  select role into first_role from public.article_credits
   where article_id = '33333333-3333-3333-3333-333333333333'
   order by position limit 1;
  if first_role <> 'photographer' then
    raise exception 'credit ordering broken: first credit is %', first_role;
  end if;
  raise notice 'ok  credits keep source order via position';
end $$;

do $$
begin
  -- storage_path is deliberately NOT unique: one stored object may back the
  -- same bytes in two articles, which is how a 1GB budget survives. A unique
  -- index here would also be a second upsert arbiter and break batch writes.
  insert into public.images
    (article_id, storage_path, public_url, thumb_url, width, height, position,
     source_image_url, content_hash)
  values ('33333333-3333-3333-3333-333333333333', 'gq/a1/01.webp',
          'https://cdn.test/3.webp', 'https://cdn.test/3t.webp', 900, 900, 3,
          'https://src.test/3.jpg', 'hash-three');
  delete from public.images where content_hash = 'hash-three';
  raise notice 'ok  a shared storage_path is allowed across rows';
end $$;

-- ---------------------------------------------------------------------------
-- C. anon — the signed-out caller holding the public bundle's key
-- ---------------------------------------------------------------------------

begin;
set local role anon;
do $$
declare t text; n int;
begin
  foreach t in array array['articles','article_credits','images','profiles',
                           'reactions','lists','list_images'] loop
    begin
      execute format('select count(*) from public.%I', t) into n;
      raise exception 'SECURITY FAIL: anon read % rows from public.%', n, t;
    exception when insufficient_privilege then null;
    end;
  end loop;
  raise notice 'ok  anon is denied on all seven tables';
end $$;
rollback;

-- ---------------------------------------------------------------------------
-- D. authenticated — user A
-- ---------------------------------------------------------------------------

begin;
select set_config('request.jwt.claims', '{"sub":"11111111-1111-1111-1111-111111111111"}', true);
set local role authenticated;

do $$
declare n int;
begin
  select count(*) into n from public.articles;
  if n <> 1 then raise exception 'signed-in user should see 1 article, saw %', n; end if;
  select count(*) into n from public.images;
  if n <> 2 then raise exception 'signed-in user should see 2 images, saw %', n; end if;
  select count(*) into n from public.article_credits;
  if n <> 2 then raise exception 'signed-in user should see 2 credits, saw %', n; end if;
  raise notice 'ok  signed-in user reads all content';
end $$;

-- Deny is deny, whether the grant layer raises or RLS quietly matches no rows.
-- Asserting on the error code alone would tie these to one layer and go green
-- if the other silently became the only thing holding.
do $$
declare n int;
begin
  begin
    insert into public.articles (source_url, category, title)
    values ('https://evil.test/x', 'news', 'injected');
    n := 1;
  exception when insufficient_privilege then n := 0;
  end;
  if n <> 0 then raise exception 'SECURITY FAIL: authenticated inserted an article'; end if;

  begin
    update public.articles set title = 'defaced';
    get diagnostics n = row_count;
  exception when insufficient_privilege then n := 0;
  end;
  if n <> 0 then raise exception 'SECURITY FAIL: authenticated updated % articles', n; end if;

  begin
    delete from public.images;
    get diagnostics n = row_count;
  exception when insufficient_privilege then n := 0;
  end;
  if n <> 0 then raise exception 'SECURITY FAIL: authenticated deleted % images', n; end if;

  -- And the content really did not move.
  if (select count(*) from public.articles where title = 'defaced') <> 0 then
    raise exception 'SECURITY FAIL: an article was defaced';
  end if;
  if (select count(*) from public.images) <> 2 then
    raise exception 'SECURITY FAIL: images were destroyed';
  end if;
  raise notice 'ok  content is read-only to signed-in users';
end $$;

do $$
declare n int;
begin
  insert into public.reactions (user_id, image_id, type)
  values ('11111111-1111-1111-1111-111111111111',
          '55555555-5555-5555-5555-555555555555', 'like');

  begin
    insert into public.reactions (user_id, image_id, type)
    values ('22222222-2222-2222-2222-222222222222',
            '55555555-5555-5555-5555-555555555555', 'dislike');
    raise exception 'SECURITY FAIL: A planted a reaction under B''s id';
  exception when insufficient_privilege then null;
  end;

  select count(*) into n from public.reactions;
  if n <> 1 then raise exception 'A should see only their 1 reaction, saw %', n; end if;

  update public.reactions set type = 'dislike'
   where user_id = '22222222-2222-2222-2222-222222222222';
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'SECURITY FAIL: A updated % of B''s reactions', n; end if;

  raise notice 'ok  reactions are owner-isolated';
end $$;

do $$
declare n int; my_list uuid;
begin
  insert into public.lists (user_id, name)
  values ('11111111-1111-1111-1111-111111111111', 'A private list')
  returning id into my_list;

  insert into public.list_images (list_id, image_id)
  values (my_list, '44444444-4444-4444-4444-444444444444');

  select count(*) into n from public.lists;
  if n <> 1 then raise exception 'A should see only their 1 list, saw %', n; end if;

  select count(*) into n from public.list_images;
  if n <> 1 then raise exception 'A should see only their 1 list entry, saw %', n; end if;

  -- with check on INSERT: cannot create a list owned by someone else.
  begin
    insert into public.lists (user_id, name)
    values ('22222222-2222-2222-2222-222222222222', 'planted');
    raise exception 'SECURITY FAIL: A planted a list under B''s id';
  exception when insufficient_privilege then null;
  end;

  -- with check on UPDATE: cannot hand your own list to someone else, and
  -- cannot capture theirs.
  begin
    update public.lists set user_id = '22222222-2222-2222-2222-222222222222'
     where id = my_list;
    raise exception 'SECURITY FAIL: A retargeted their list to B';
  exception when insufficient_privilege then null;
  end;

  -- The interesting one: list_images has no user_id, so ownership has to come
  -- from the parent list.
  begin
    insert into public.list_images (list_id, image_id)
    values ('66666666-6666-6666-6666-666666666666',
            '55555555-5555-5555-5555-555555555555');
    raise exception 'SECURITY FAIL: A added an image to B''s list';
  exception when insufficient_privilege then null;
  end;

  begin
    update public.list_images set list_id = '66666666-6666-6666-6666-666666666666'
     where list_id = my_list;
    get diagnostics n = row_count;
    if n <> 0 then raise exception 'SECURITY FAIL: A moved % rows into B''s list', n; end if;
  exception when insufficient_privilege then null;
  end;

  delete from public.lists where user_id = '22222222-2222-2222-2222-222222222222';
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'SECURITY FAIL: A deleted % of B''s lists', n; end if;

  raise notice 'ok  lists and list contents are owner-isolated';
end $$;

do $$
declare n int;
begin
  update public.profiles set display_name = 'Renamed A'
   where id = '11111111-1111-1111-1111-111111111111';
  get diagnostics n = row_count;
  if n <> 1 then raise exception 'A could not rename their own profile'; end if;

  update public.profiles set display_name = 'hacked'
   where id = '22222222-2222-2222-2222-222222222222';
  get diagnostics n = row_count;
  if n <> 0 then raise exception 'SECURITY FAIL: A renamed B''s profile'; end if;

  -- Reading the roster is allowed; PLAN.md scopes profile reads to any
  -- signed-in user so a future "saved by" label has something to show.
  select count(*) into n from public.profiles;
  if n <> 2 then raise exception 'A should see 2 profiles, saw %', n; end if;

  raise notice 'ok  profiles readable, writable only by their owner';
end $$;
rollback;

-- ---------------------------------------------------------------------------
-- E. authenticated role, no usable JWT
--
-- PostgREST only hands out this role for a validly signed token, so this is
-- belt-and-braces: it pins that a NULL auth.uid() matches nobody's rows rather
-- than everybody's.
-- ---------------------------------------------------------------------------

begin;
set local role authenticated;
do $$
declare t text; n int;
begin
  if auth.uid() is not null then
    raise exception 'expected a null auth.uid() with no claims set, got %', auth.uid();
  end if;
  foreach t in array array['reactions','lists','list_images'] loop
    execute format('select count(*) from public.%I', t) into n;
    if n <> 0 then
      raise exception 'SECURITY FAIL: a session with no JWT read % rows from public.%', n, t;
    end if;
  end loop;
  raise notice 'ok  a null auth.uid() owns nothing';
end $$;
rollback;

-- ---------------------------------------------------------------------------
-- F. service_role — the scraper's key, held in GitHub Actions secrets
-- ---------------------------------------------------------------------------

begin;
set local role service_role;
do $$
declare t text; n int;
begin
  insert into public.articles (id, source_url, category, title, published_date)
  values ('77777777-7777-7777-7777-777777777777',
          'https://www.gqkorea.co.kr/test-a2', 'sneakers', '여름의 하이탑', '2026-06-28');
  insert into public.images
    (article_id, storage_path, public_url, thumb_url, width, height, position,
     source_image_url, content_hash)
  values ('77777777-7777-7777-7777-777777777777', 'gq/a2/01.webp',
          'https://cdn.test/a2.webp', 'https://cdn.test/a2t.webp', 1200, 1600, 1,
          'https://src.test/a2.jpg', 'hash-a2');
  insert into public.article_credits (article_id, position, role_raw, role, person_name)
  values ('77777777-7777-7777-7777-777777777777', 1, '포토그래퍼', 'photographer', '이준호');

  if (select count(*) from public.articles) <> 2 then
    raise exception 'service_role could not write articles';
  end if;

  foreach t in array array['reactions','lists','list_images','profiles'] loop
    begin
      execute format('select count(*) from public.%I', t) into n;
      raise exception 'SECURITY FAIL: the scraper key read % rows from public.%', n, t;
    exception when insufficient_privilege then null;
    end;
  end loop;

  raise notice 'ok  scraper key writes all content, cannot touch curation';
end $$;
rollback;

-- ---------------------------------------------------------------------------
-- G. Storage
-- ---------------------------------------------------------------------------

do $$
declare b record;
begin
  select * into b from storage.buckets where id = 'gallery';
  if b is null then raise exception 'storage bucket "gallery" was not created'; end if;
  if not b.public then
    raise exception 'gallery bucket is private — <img src> and browser caching both break';
  end if;
  if b.file_size_limit is null or b.file_size_limit > 10485760 then
    raise exception 'gallery bucket has no sane size cap: %', b.file_size_limit;
  end if;
  if not (b.allowed_mime_types @> array['image/webp']) then
    raise exception 'gallery bucket does not accept image/webp: %', b.allowed_mime_types;
  end if;
  raise notice 'ok  gallery bucket is public, size-capped and webp-restricted';
end $$;

do $$
declare n int;
begin
  -- A public bucket serves reads without auth by design, but nobody except
  -- service_role may WRITE objects. storage.objects ships with RLS on and no
  -- policies; a policy added later would silently widen that.
  if not (select relrowsecurity from pg_class where oid = 'storage.objects'::regclass) then
    raise exception 'SECURITY FAIL: RLS is not enabled on storage.objects';
  end if;
  select count(*) into n from pg_policies
   where schemaname = 'storage' and tablename = 'objects';
  if n <> 0 then
    raise exception 'SECURITY FAIL: % unexpected policies on storage.objects', n;
  end if;
  raise notice 'ok  storage.objects is deny-all except service_role';
end $$;

\echo ''
\echo 'ALL RLS TESTS PASSED'
