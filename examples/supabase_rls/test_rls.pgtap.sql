-- pgTAP port of Property 4 from `test_rls.py`
-- (`test_outsider_cannot_read_drafts_in_another_org`).
--
-- Same property, written in the conventional pgTAP idiom so you can
-- read both side-by-side. Only this one property is ported — the file is
-- a comparison artifact, not a full pgTAP suite.
--
-- Run:
--     psql "$SQLPROOF_TEST_DATABASE_URL" -f test_rls.pgtap.sql
--
-- Requires the example schema to already be applied. Wrapped in
-- BEGIN…ROLLBACK so re-runs are safe.
--
-- Where the verbosity goes:
--   1. No data generation. Every UUID and column value is hardcoded;
--      sqlproof generates the dataset from `sizes` + `columns`.
--   2. Property coverage by enumeration. The property holds for any
--      (member_role, is_premium) pair, so the 6-cell matrix is hand-
--      written. With sqlproof, 20 randomized runs cover the matrix for
--      free.
--   3. Auth-context dance inline at every transition: set claims,
--      `SET LOCAL ROLE authenticated`, `RESET ROLE`. sqlproof's
--      `as_rls_user` collapses this to one `with` line.
--   4. Sanity checks must be explicit. Without "the actual member can
--      see their own draft" assertions, an over-restrictive RLS policy
--      that hides everything would make the negative assertions pass
--      for the wrong reason.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;

-- 6 attack assertions + 6 sanity assertions = 12.
SELECT plan(12);

-- ---------------------------------------------------------------------------
-- Fixtures
-- ---------------------------------------------------------------------------

-- ON CONFLICT DO NOTHING: pgTAP scripts often share a Supabase DB with
-- other tests, and `auth.users` rows persist outside this transaction's
-- scope (the BEGIN…ROLLBACK doesn't roll back pre-existing rows).
INSERT INTO auth.users (id, aud, role, email) VALUES
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'authenticated', 'authenticated', 'pgtap_attacker@test.invalid'),
  ('00000001-0001-0001-0001-000000000001', 'authenticated', 'authenticated', 'pgtap_owner@test.invalid'),
  ('00000002-0002-0002-0002-000000000002', 'authenticated', 'authenticated', 'pgtap_admin@test.invalid'),
  ('00000003-0003-0003-0003-000000000003', 'authenticated', 'authenticated', 'pgtap_editor@test.invalid')
ON CONFLICT (id) DO NOTHING;

INSERT INTO organizations (id, name, plan_type, max_posts) VALUES
  ('01010101-0101-0101-0101-010101010101', 'Owner Org',  'pro', 100),
  ('02020202-0202-0202-0202-020202020202', 'Admin Org',  'pro', 100),
  ('03030303-0303-0303-0303-030303030303', 'Editor Org', 'pro', 100);

INSERT INTO org_members (org_id, user_id, role) VALUES
  ('01010101-0101-0101-0101-010101010101', '00000001-0001-0001-0001-000000000001', 'owner'),
  ('02020202-0202-0202-0202-020202020202', '00000002-0002-0002-0002-000000000002', 'admin'),
  ('03030303-0303-0303-0303-030303030303', '00000003-0003-0003-0003-000000000003', 'editor');

-- 3 orgs * (free, premium) = 6 drafts.
INSERT INTO posts (id, org_id, author_id, status, is_premium) VALUES
  ('11111111-1111-1111-1111-111111111111', '01010101-0101-0101-0101-010101010101', '00000001-0001-0001-0001-000000000001', 'draft', false),
  ('11111112-1111-1111-1111-111111111112', '01010101-0101-0101-0101-010101010101', '00000001-0001-0001-0001-000000000001', 'draft', true),
  ('22222221-2222-2222-2222-222222222221', '02020202-0202-0202-0202-020202020202', '00000002-0002-0002-0002-000000000002', 'draft', false),
  ('22222222-2222-2222-2222-222222222222', '02020202-0202-0202-0202-020202020202', '00000002-0002-0002-0002-000000000002', 'draft', true),
  ('33333331-3333-3333-3333-333333333331', '03030303-0303-0303-0303-030303030303', '00000003-0003-0003-0003-000000000003', 'draft', false),
  ('33333333-3333-3333-3333-333333333333', '03030303-0303-0303-0303-030303030303', '00000003-0003-0003-0003-000000000003', 'draft', true);

-- ---------------------------------------------------------------------------
-- Sanity: each member can see both of their own drafts.
-- Without these checks, an RLS policy that hides ALL rows from everyone
-- would pass the negative assertions further down — a false sense of
-- security.
-- ---------------------------------------------------------------------------

-- Owner
SELECT set_config(
  'request.jwt.claims',
  '{"sub":"00000001-0001-0001-0001-000000000001","role":"authenticated"}',
  true
);
SET LOCAL ROLE authenticated;

SELECT isnt_empty(
  $$SELECT id FROM posts WHERE id = '11111111-1111-1111-1111-111111111111'$$,
  'sanity: owner sees own free draft'
);
SELECT isnt_empty(
  $$SELECT id FROM posts WHERE id = '11111112-1111-1111-1111-111111111112'$$,
  'sanity: owner sees own premium draft'
);

RESET ROLE;

-- Admin
SELECT set_config(
  'request.jwt.claims',
  '{"sub":"00000002-0002-0002-0002-000000000002","role":"authenticated"}',
  true
);
SET LOCAL ROLE authenticated;

SELECT isnt_empty(
  $$SELECT id FROM posts WHERE id = '22222221-2222-2222-2222-222222222221'$$,
  'sanity: admin sees own free draft'
);
SELECT isnt_empty(
  $$SELECT id FROM posts WHERE id = '22222222-2222-2222-2222-222222222222'$$,
  'sanity: admin sees own premium draft'
);

RESET ROLE;

-- Editor
SELECT set_config(
  'request.jwt.claims',
  '{"sub":"00000003-0003-0003-0003-000000000003","role":"authenticated"}',
  true
);
SET LOCAL ROLE authenticated;

SELECT isnt_empty(
  $$SELECT id FROM posts WHERE id = '33333331-3333-3333-3333-333333333331'$$,
  'sanity: editor sees own free draft'
);
SELECT isnt_empty(
  $$SELECT id FROM posts WHERE id = '33333333-3333-3333-3333-333333333333'$$,
  'sanity: editor sees own premium draft'
);

RESET ROLE;

-- ---------------------------------------------------------------------------
-- Property: an outsider — not a member of any of the three orgs — cannot
-- read any of the six drafts.
-- ---------------------------------------------------------------------------

SELECT set_config(
  'request.jwt.claims',
  '{"sub":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","role":"authenticated"}',
  true
);
SET LOCAL ROLE authenticated;

SELECT is_empty(
  $$SELECT id FROM posts WHERE id = '11111111-1111-1111-1111-111111111111'$$,
  'outsider cannot read owner-org free draft'
);
SELECT is_empty(
  $$SELECT id FROM posts WHERE id = '11111112-1111-1111-1111-111111111112'$$,
  'outsider cannot read owner-org premium draft'
);
SELECT is_empty(
  $$SELECT id FROM posts WHERE id = '22222221-2222-2222-2222-222222222221'$$,
  'outsider cannot read admin-org free draft'
);
SELECT is_empty(
  $$SELECT id FROM posts WHERE id = '22222222-2222-2222-2222-222222222222'$$,
  'outsider cannot read admin-org premium draft'
);
SELECT is_empty(
  $$SELECT id FROM posts WHERE id = '33333331-3333-3333-3333-333333333331'$$,
  'outsider cannot read editor-org free draft'
);
SELECT is_empty(
  $$SELECT id FROM posts WHERE id = '33333333-3333-3333-3333-333333333333'$$,
  'outsider cannot read editor-org premium draft'
);

RESET ROLE;

-- ---------------------------------------------------------------------------
-- Done
-- ---------------------------------------------------------------------------

SELECT * FROM finish();
ROLLBACK;
