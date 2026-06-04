-- Recipe 10 fix: restrict DELETE to (a) the caller deleting their own
-- row, or (b) an admin in the same org deleting another member.
--
-- The inner subquery references `org_members` from within an `org_members`
-- RLS policy, which would normally trigger infinite RLS recursion. We
-- avoid this by routing the admin-check through the `is_admin_in_org`
-- SECURITY DEFINER helper (shipped in 001_initial.sql alongside Recipe 10),
-- which bypasses RLS internally.

DROP POLICY IF EXISTS "members manage their own row delete" ON org_members;

CREATE OR REPLACE FUNCTION is_admin_in_org(p_org_id UUID, p_user_id UUID)
  RETURNS BOOLEAN
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM org_members
    WHERE user_id = p_user_id AND org_id = p_org_id AND role = 'admin'
  );
$$;

GRANT EXECUTE ON FUNCTION is_admin_in_org(UUID, UUID) TO authenticated;

CREATE POLICY "members manage their own row delete" ON org_members
  FOR DELETE TO authenticated
  USING (
    org_members.user_id = auth.uid()
    OR is_admin_in_org(org_members.org_id, auth.uid())
  );
