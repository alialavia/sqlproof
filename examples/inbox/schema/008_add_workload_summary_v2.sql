-- Recipe 7: ships the "optimized" v2 — a single scan with FILTER
-- aggregations.
--
-- BUG: uses INNER JOIN instead of LEFT JOIN. Agents with zero
-- assigned tickets are silently dropped from the result, where v1
-- returns them as `(agent_id, 0, 0, 0)`. Code review reads this as
-- "join agents to their tickets, group, aggregate" and the missing
-- LEFT is invisible.

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
  JOIN tickets t ON t.assigned_agent_id = m.user_id    -- BUG: missing LEFT
  WHERE m.org_id = p_org_id
    AND m.role   = 'agent'
  GROUP BY m.user_id;
$$;

GRANT EXECUTE ON FUNCTION agent_workload_summary_v2(UUID) TO authenticated;
