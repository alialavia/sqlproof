-- Recipe 6 fix: two changes to make pagination stable.
--
-- 1. DISTINCT ON (a.id) in the inner query collapses the
--    one-article-many-embedding-chunks JOIN fanout: without it,
--    an article with N chunks appears N times, inflating page sizes
--    and making the no-duplicates property unverifiable.
--    The inner ORDER BY (a.id, score DESC) picks the best-scoring
--    chunk per article.
--
-- 2. ORDER BY score DESC, article_id ASC in the outer query adds
--    `article_id` as a tiebreaker so the sort is total. Without it,
--    Postgres's tie-breaking for equal scores is implementation-defined;
--    the same article can appear on two pages or vanish entirely.

CREATE OR REPLACE FUNCTION search_kb_hybrid(
  p_org_id           UUID,
  p_query_embedding  vector(384),
  p_text_query       TEXT,
  p_limit            INT DEFAULT 5,
  p_offset           INT DEFAULT 0
)
  RETURNS TABLE (article_id UUID, score double precision)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT article_id, score
  FROM (
    SELECT DISTINCT ON (a.id)
      a.id AS article_id,
      (
        0.7 * (1 - (ae.embedding <=> p_query_embedding))
        + 0.3 * CASE WHEN a.title ILIKE '%' || p_text_query || '%' THEN 1.0 ELSE 0.0 END
      ) AS score
    FROM kb_articles a
    JOIN kb_article_embeddings ae ON ae.article_id = a.id
    WHERE a.org_id = p_org_id
    ORDER BY a.id,
             (
               0.7 * (1 - (ae.embedding <=> p_query_embedding))
               + 0.3 * CASE WHEN a.title ILIKE '%' || p_text_query || '%' THEN 1.0 ELSE 0.0 END
             ) DESC
  ) best_per_article
  ORDER BY score DESC, article_id ASC   -- total order: no ties, stable pagination
  LIMIT p_limit OFFSET p_offset;
$$;

GRANT EXECUTE ON FUNCTION search_kb_hybrid(UUID, vector, TEXT, INT, INT) TO authenticated;
