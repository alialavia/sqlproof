-- Recipe 4 fix: move the org_id filter into the JOIN condition so the
-- LEFT JOIN really is a LEFT JOIN.

CREATE OR REPLACE FUNCTION organization_dashboard(p_org_id UUID)
  RETURNS TABLE (status ticket_status, count BIGINT)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT s.status, count(t.id)
  FROM unnest(enum_range(NULL::ticket_status)) AS s(status)
  LEFT JOIN tickets t
    ON t.status = s.status
   AND t.org_id = p_org_id
  GROUP BY s.status;
$$;

GRANT EXECUTE ON FUNCTION organization_dashboard(UUID) TO authenticated;
