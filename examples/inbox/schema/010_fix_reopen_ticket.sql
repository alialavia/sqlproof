-- Recipe 8 fix: clear resolved_at when reopening.

CREATE OR REPLACE FUNCTION reopen_ticket(p_ticket_id UUID)
  RETURNS VOID
  LANGUAGE sql
  SECURITY DEFINER
  SET search_path = public
AS $$
  UPDATE tickets
     SET status      = 'reopened',
         resolved_at = NULL
   WHERE id = p_ticket_id;
$$;
