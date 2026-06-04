-- Recipe 3 fix: only set resolved_at when the status *transitions*
-- into 'resolved' from something else.

CREATE OR REPLACE FUNCTION tg_close_sets_resolved_at()
  RETURNS TRIGGER
  LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.status = 'resolved'
     AND (OLD IS NULL OR OLD.status IS DISTINCT FROM 'resolved')
  THEN
    NEW.resolved_at := now();
  END IF;
  RETURN NEW;
END;
$$;
