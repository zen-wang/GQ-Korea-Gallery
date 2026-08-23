-- Take EXECUTE on the trigger functions away from the API roles.
--
-- Found by Supabase's own security advisor after the first two migrations were
-- applied (lints 0028/0029), and missed by our review: Postgres grants EXECUTE
-- on a new function to PUBLIC by default, and Supabase's default privileges add
-- anon and authenticated on top. All four functions were therefore reachable at
-- /rest/v1/rpc/<name>.
--
-- 20260822120100 disarmed those default privileges, but ALTER DEFAULT
-- PRIVILEGES only applies to objects created afterwards — these four already
-- existed by then. That ordering gap is the actual bug.
--
-- Exploitability is close to nil: all four are `returns trigger`, and Postgres
-- refuses to run a trigger function outside a trigger. This is defence in
-- depth, and cheap. handle_new_user() in particular is SECURITY DEFINER and
-- runs as the table owner, which is exactly the shape worth closing off.
--
-- Firing a trigger does not re-check EXECUTE (that privilege is checked when
-- the trigger is created), so the triggers keep working. The suite in
-- supabase/tests/ proves it: each of these functions has a test that goes red
-- if it stops firing.

revoke all on function public.handle_new_user()
  from public, anon, authenticated;
revoke all on function public.set_updated_at()
  from public, anon, authenticated;
revoke all on function public.images_inherit_published_date()
  from public, anon, authenticated;
revoke all on function public.articles_propagate_published_date()
  from public, anon, authenticated;
