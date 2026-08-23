-- Minimal stand-ins for the parts of a Supabase project the migrations touch,
-- so the schema can be applied to a throwaway local Postgres and tested.
-- NOT part of the real project — Supabase provides all of this itself.
--
-- Fidelity matters more than brevity here. A stub that is more permissive than
-- production makes tests fail that should pass, which is annoying. A stub that
-- is more RESTRICTIVE makes tests pass that should fail, which is how a revoke
-- that does nothing in production looks green on a laptop.

create role anon          nologin noinherit;
create role authenticated nologin noinherit;
create role service_role  nologin noinherit bypassrls;

-- Stand-in for supabase_auth_admin, the role the auth service inserts users
-- as. It deliberately gets no rights on public.*, which is exactly what makes
-- the `security definer` on handle_new_user() load-bearing rather than
-- decorative.
create role auth_admin    nologin noinherit;

grant usage on schema public to anon, authenticated, service_role;

-- The lines that make the migrations' revokes mean anything. Every Supabase
-- project carries these, so each `create table` in a migration immediately
-- grants ALL to anon and authenticated on a table whose RLS is still off.
-- Without them here, the tests would validate a privilege state that cannot
-- exist on the target platform.
alter default privileges for role postgres in schema public
  grant all on tables    to postgres, anon, authenticated, service_role;
alter default privileges for role postgres in schema public
  grant all on sequences to postgres, anon, authenticated, service_role;
alter default privileges for role postgres in schema public
  grant all on functions to postgres, anon, authenticated, service_role;

create schema if not exists auth;

create table auth.users (
  id                  uuid primary key default gen_random_uuid(),
  email               text unique,
  raw_user_meta_data  jsonb not null default '{}'::jsonb
);

-- Supabase's own implementation. The nullif() sits OUTSIDE the jsonb cast on
-- purpose: with an empty claims string, a cast-first version raises
-- invalid_text_representation where the real one returns NULL — which would
-- make the "signed-in role holding no JWT" case untestable.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'
  )::uuid;
$$;

grant usage on schema auth to anon, authenticated, service_role, auth_admin;
grant execute on function auth.uid() to anon, authenticated, service_role;
grant insert, select on table auth.users to auth_admin;
