-- Recipe 1 fix: scope the search to the input ticket's org_id.

CREATE OR REPLACE FUNCTION find_similar_tickets(
  p_ticket_id UUID,
  p_k INT DEFAULT 5
)
  RETURNS TABLE (ticket_id UUID, distance double precision)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  WITH input AS (
    SELECT t.org_id, me.embedding
    FROM tickets t
    JOIN messages m            ON m.ticket_id = t.id
    JOIN message_embeddings me ON me.message_id = m.id
    WHERE t.id = p_ticket_id
    LIMIT 1
  )
  SELECT m.ticket_id,
         (me.embedding <-> (SELECT embedding FROM input)) AS distance
  FROM message_embeddings me
  JOIN messages m ON m.id = me.message_id
  JOIN tickets  t ON t.id = m.ticket_id
  WHERE t.org_id    = (SELECT org_id FROM input)
    AND m.ticket_id <> p_ticket_id
  ORDER BY distance ASC
  LIMIT p_k;
$$;

GRANT EXECUTE ON FUNCTION find_similar_tickets(UUID, INT) TO authenticated;
