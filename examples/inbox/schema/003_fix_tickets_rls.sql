-- Recipe 2 fix: correlate the EXISTS subquery back to `tickets.org_id`.
--
-- Before: `EXISTS (SELECT 1 FROM org_members WHERE user_id = auth.uid())`
--         — fires true for any authenticated org member.
-- After:  `EXISTS (SELECT 1 FROM org_members WHERE user_id = auth.uid()
--                  AND org_id = tickets.org_id)` — fires true only when
--         the caller is a member of *this ticket's* org.

DROP POLICY IF EXISTS "agents see org tickets" ON tickets;

CREATE POLICY "agents see org tickets" ON tickets
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM org_members
      WHERE org_members.user_id = auth.uid()
        AND org_members.org_id  = tickets.org_id
    )
  );
