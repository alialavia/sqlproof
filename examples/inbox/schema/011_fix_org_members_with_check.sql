-- Recipe 9 fix: add a WITH CHECK clause that pins the role to 'viewer'.
--
-- WITH CHECK is evaluated against the *new* row state. The simplest
-- correct constraint is "you can only touch your own row, and the
-- result must still be a viewer row" — a viewer's no-op self-update
-- still passes, but escalating to 'admin' or 'agent' fails.
--
-- This pattern is enum-stable: any future role added to the
-- `member_role` enum is automatically denied by the `= 'viewer'`
-- check, without needing this policy to be updated.
--
-- Role *promotions* must go through a SECURITY DEFINER admin
-- function instead — that's the standard Supabase pattern for
-- privileged mutations.

DROP POLICY IF EXISTS "members manage their own row" ON org_members;

CREATE POLICY "members manage their own row" ON org_members
  FOR UPDATE TO authenticated
  USING      (org_members.user_id = auth.uid())
  WITH CHECK (
    org_members.user_id = auth.uid()
    AND org_members.role = 'viewer'
  );
