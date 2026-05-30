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
