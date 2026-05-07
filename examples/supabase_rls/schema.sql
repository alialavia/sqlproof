-- Orgs / org_members / posts schema for the SqlProof Supabase RLS example.
--
-- Assumes a Supabase-shaped database where these already exist:
--   * `auth.users` (table) — Supabase's user table
--   * `auth.uid()` (function) — reads the JWT `sub` claim
--   * `authenticated` (role) — what user-context queries run as
--
-- Tests will set `request.jwt.claims` so `auth.uid()` resolves to the
-- intended user, and `SET LOCAL ROLE authenticated` so RLS actually
-- applies (the connection's superuser role would otherwise bypass it).

-- ---------------------------------------------------------------------------
-- Application schema
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS organizations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  plan_type TEXT NOT NULL CHECK (plan_type IN ('free', 'pro', 'enterprise')),
  max_posts INTEGER NOT NULL CHECK (max_posts >= 0)
);

CREATE TABLE IF NOT EXISTS org_members (
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'viewer')),
  PRIMARY KEY (org_id, user_id)
);

CREATE TABLE IF NOT EXISTS posts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  author_id UUID NOT NULL REFERENCES auth.users(id),
  status TEXT NOT NULL CHECK (status IN ('draft', 'published', 'archived')),
  is_premium BOOLEAN NOT NULL DEFAULT false
);

-- ---------------------------------------------------------------------------
-- RLS helpers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.get_user_org_role(p_org_id UUID, p_user_id UUID)
  RETURNS TEXT
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = ''
AS $$
  SELECT role FROM public.org_members
  WHERE org_id = p_org_id AND user_id = p_user_id;
$$;

CREATE OR REPLACE FUNCTION public.can_add_post(p_org_id UUID)
  RETURNS BOOLEAN
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = ''
AS $$
  SELECT (SELECT count(*) FROM public.posts WHERE org_id = p_org_id) < o.max_posts
  FROM public.organizations o WHERE o.id = p_org_id;
$$;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

GRANT SELECT, INSERT, UPDATE, DELETE ON organizations TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON org_members TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON posts TO authenticated;
GRANT EXECUTE ON FUNCTION public.get_user_org_role(UUID, UUID) TO authenticated;
GRANT EXECUTE ON FUNCTION public.can_add_post(UUID) TO authenticated;

-- ---------------------------------------------------------------------------
-- Row-Level Security
-- ---------------------------------------------------------------------------

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE posts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Members visible to org members" ON org_members;
CREATE POLICY "Members visible to org members" ON org_members FOR SELECT
  USING (public.get_user_org_role(org_id, auth.uid()) IS NOT NULL);

DROP POLICY IF EXISTS "Member management restricted to admins and owners" ON org_members;
CREATE POLICY "Member management restricted to admins and owners" ON org_members FOR ALL
  USING (public.get_user_org_role(org_id, auth.uid()) IN ('owner', 'admin'));

DROP POLICY IF EXISTS "Complex post visibility" ON posts;
CREATE POLICY "Complex post visibility" ON posts FOR SELECT USING (
  (status = 'published' AND NOT is_premium)
  OR (status = 'published' AND is_premium AND public.get_user_org_role(org_id, auth.uid()) IS NOT NULL)
  OR public.get_user_org_role(org_id, auth.uid()) IN ('owner', 'admin', 'editor')
);

DROP POLICY IF EXISTS "Post creation rules" ON posts;
CREATE POLICY "Post creation rules" ON posts FOR INSERT WITH CHECK (
  public.get_user_org_role(org_id, auth.uid()) IN ('owner', 'admin', 'editor')
  AND (
    (SELECT plan_type FROM organizations WHERE id = org_id) <> 'free'
    OR public.can_add_post(org_id)
  )
);
