-- Recipe 7 fix: change INNER JOIN to LEFT JOIN so agents with zero
-- assigned tickets are preserved as a one-row group with all-NULL
-- ticket fields. count(*) FILTER (...) returns 0 over such groups,
-- matching v1's "0 instead of dropped" contract.
--
-- NOTE: the filename has historical "v2_nulls" framing reflecting an
-- earlier hypothesis that the divergence was about NULL aggregate
-- handling. The actual bug turned out to be the join type. The file
-- name is preserved for spec alignment; the content reflects what
-- actually fixes the divergence.

CREATE OR REPLACE FUNCTION agent_workload_summary_v2(p_org_id UUID)
  RETURNS TABLE (
    user_id          UUID,
    open_count       BIGINT,
    pending_count    BIGINT,
    sla_breach_count BIGINT
  )
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT
    m.user_id,
    count(*) FILTER (WHERE t.status = 'open')    AS open_count,
    count(*) FILTER (WHERE t.status = 'pending') AS pending_count,
    count(*) FILTER (
      WHERE t.sla_due_at IS NOT NULL
        AND t.resolved_at IS NOT NULL
        AND t.sla_due_at < t.resolved_at
    ) AS sla_breach_count
  FROM org_members m
  LEFT JOIN tickets t ON t.assigned_agent_id = m.user_id    -- the fix
  WHERE m.org_id = p_org_id
    AND m.role   = 'agent'
  GROUP BY m.user_id;
$$;
