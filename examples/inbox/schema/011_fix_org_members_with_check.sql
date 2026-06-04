-- Recipe 9 fix: add a WITH CHECK clause that pins the role.
--
-- WITH CHECK is evaluated against the *new* row state; the simplest
-- correct constraint is "you can only update your own row, and you
-- can't change the role." A SELECT subquery against the same table
-- to verify the role is unchanged would cause infinite RLS recursion;
-- the practical alternative is to deny role changes entirely via WITH
-- CHECK on `role`. Role escalation should go through a
-- SECURITY DEFINER admin function instead.

DROP POLICY IF EXISTS "members manage their own row" ON org_members;

CREATE POLICY "members manage their own row" ON org_members
  FOR UPDATE TO authenticated
  USING      (org_members.user_id = auth.uid())
  WITH CHECK (
    org_members.user_id = auth.uid()
    AND org_members.role NOT IN ('admin', 'agent')
  );
