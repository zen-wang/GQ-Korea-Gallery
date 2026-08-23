-- Move image hosting from Cloudflare R2 to Supabase Storage.
--
-- Why: R2 requires a payment method on file to activate, even inside its free
-- tier. Supabase Storage needs no card, is already part of this project, and
-- removes a vendor plus five CI secrets. The cost is a 1 GB ceiling instead of
-- R2's 10 GB — roughly 3,000-3,500 images at PLAN.md's WebP/1600px settings.
-- PHASE0_AMENDMENTS §E records the decision.
--
-- The column is renamed to `storage_path` rather than kept as `r2_key` so the
-- schema stops naming a vendor. If this ever moves again (Backblaze B2 is the
-- likely next step if the 1 GB ceiling bites), only the uploader and the stored
-- URLs change — not the shape of the table.

alter table public.images rename column r2_key to storage_path;
alter index images_r2_key_idx rename to images_storage_path_idx;

comment on table public.images is
  'One row per body image, re-hosted in Supabase Storage. Dedupe key: (article_id, content_hash).';
comment on column public.images.storage_path is
  'Path within the gallery bucket, e.g. <article_id>/<hash>.webp. Not unique: identical bytes in two articles may share one stored object.';

-- The bucket. Public, with unguessable paths — the posture PHASE0_AMENDMENTS
-- §E already chose for R2, kept deliberately:
--   * <img src> works directly, and the browser caches by URL. Signed URLs are
--     re-minted per session, which busts that cache on every visit — bad for a
--     phone-first gallery whose whole bandwidth story (§B.4) is thumbnails.
--   * The gallery itself stays auth-gated. Reading an image requires knowing a
--     path prefixed by a v4 UUID, and storage.objects has RLS enabled with no
--     policies, so the bucket cannot be listed or enumerated.
-- Private-bucket + signed URLs remains the hardening option §E named, if the
-- privacy bar ever rises.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'gallery',
  'gallery',
  true,
  10485760,                       -- 10 MB; optimized WebP lands near 250 KB, so
                                  -- anything near this is a pipeline bug
  array['image/webp', 'image/jpeg']
)
on conflict (id) do update
   set public             = excluded.public,
       file_size_limit    = excluded.file_size_limit,
       allowed_mime_types = excluded.allowed_mime_types;

-- No policies on storage.objects on purpose. It ships with RLS enabled and no
-- policies, which denies every write to anon and authenticated. The scraper
-- uploads as service_role, which bypasses RLS. Adding a policy here would only
-- widen that.
