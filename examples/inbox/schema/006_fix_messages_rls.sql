-- Recipe 5 fix: gate internal messages on the agent/admin path only.
--
-- Customers retain access to non-internal messages on their tickets;
-- internal notes are visible only to org members.

DROP POLICY IF EXISTS "messages visible with parent ticket" ON messages;

CREATE POLICY "messages visible with parent ticket" ON messages
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM tickets t
      WHERE t.id = messages.ticket_id
        AND (
          EXISTS (
            SELECT 1 FROM org_members om
            WHERE om.user_id = auth.uid()
              AND om.org_id  = t.org_id
          )
          OR (
            nullif(auth.jwt() ->> 'customer_id', '')::uuid = t.customer_id
            AND messages.is_internal = false   -- the missing gate
          )
        )
    )
  );
