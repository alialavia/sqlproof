-- Bring a bare `supabase/postgres` container into lockstep with managed
-- Supabase semantics, so SqlProof's RLS-aware helpers (auth.uid(),
-- auth.role(), as_rls_user) behave the same way they would in production.
--
-- This applies two things the bare image is missing:
--
-- 1. `plpgsql_check` extension. The binary ships with the image but isn't
--    installed in the default database. Needed by
--    sqlproof.contrib.plpgsql_coverage.
--
-- 2. GoTrue's `20220224000811_update_auth_functions` migration. The bare
--    image's `auth.uid()` only reads the legacy singular GUC
--    `request.jwt.claim.sub`. PostgREST 8+ (and SqlProof's
--    `as_rls_user` helper) write the modern JSON `request.jwt.claims`
--    GUC. Managed Supabase patches this at deploy time via GoTrue; we
--    apply the same migration here so the bare image matches.
--
--    Source: https://github.com/supabase/auth/blob/master/migrations/20220224000811_update_auth_functions.up.sql

CREATE EXTENSION IF NOT EXISTS plpgsql_check;

CREATE OR REPLACE FUNCTION auth.uid()
RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    NULLIF(current_setting('request.jwt.claim.sub', true), ''),
    (NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid
$$;

CREATE OR REPLACE FUNCTION auth.role()
RETURNS text LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    NULLIF(current_setting('request.jwt.claim.role', true), ''),
    (NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
  )::text
$$;

CREATE OR REPLACE FUNCTION auth.email()
RETURNS text LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    NULLIF(current_setting('request.jwt.claim.email', true), ''),
    (NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'email')
  )::text
$$;

CREATE OR REPLACE FUNCTION auth.jwt()
RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    NULLIF(current_setting('request.jwt.claim', true), ''),
    NULLIF(current_setting('request.jwt.claims', true), '')
  )::jsonb
$$;

-- ---------------------------------------------------------------------------
-- storage.buckets — bring up to columns added by supabase-api migrations
-- 0008+ so user code that INSERTs newer columns (id, name, public, type,
-- file_size_limit, etc.) applies cleanly on the bare image.
--
-- The bare image ships storage.buckets with just (id, name, owner,
-- created_at, updated_at). Newer storage migrations add the columns
-- below. We apply the minimum-viable subset (column additions only,
-- not the analytics/iceberg tables or RLS policies from migration 0038).
--
-- Sources (mapped one-to-one to columns below):
-- - 0008 (public)              https://github.com/supabase/storage-api/blob/master/migrations/tenant/0008-add-public-to-buckets.sql
-- - 0012 (avif_autodetection)  .../0012-add-automatic-avif-detection-flag.sql
-- - 0013+0014 (file_size_limit) .../0013-add-bucket-custom-limits.sql .../0014-use-bytes-for-max-size.sql
-- - 0013 (allowed_mime_types)  .../0013-add-bucket-custom-limits.sql
-- - 0018 (owner_id)            .../0018-add_owner_id_column_deprecate_owner.sql
-- - 0038 (BucketType + type)   .../0038-iceberg-catalog-flag-on-buckets.sql
-- - 0044 (VECTOR enum value)   .../0044-vector-bucket-type.sql

ALTER TABLE storage.buckets
  ADD COLUMN IF NOT EXISTS public boolean DEFAULT false;

ALTER TABLE storage.buckets
  ADD COLUMN IF NOT EXISTS avif_autodetection boolean DEFAULT false;

ALTER TABLE storage.buckets
  ADD COLUMN IF NOT EXISTS file_size_limit bigint DEFAULT NULL;

ALTER TABLE storage.buckets
  ADD COLUMN IF NOT EXISTS allowed_mime_types text[] DEFAULT NULL;

ALTER TABLE storage.buckets
  ADD COLUMN IF NOT EXISTS owner_id text DEFAULT NULL;

-- BucketType enum and the type column. Includes VECTOR (added in
-- migration 0044) so a single CREATE TYPE covers all current values.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typname = 'buckettype' AND n.nspname = 'storage'
  ) THEN
    CREATE TYPE storage.BucketType AS ENUM ('STANDARD', 'ANALYTICS', 'VECTOR');
  END IF;
END$$;

ALTER TABLE storage.buckets
  ADD COLUMN IF NOT EXISTS type storage.BucketType NOT NULL DEFAULT 'STANDARD';
