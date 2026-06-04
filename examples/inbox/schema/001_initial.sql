-- Inbox sample: tables, RLS enablement, and grants.
--
-- This file ships ten tables plus the buggy RPCs, RLS policies, and
-- trigger that the recipes' tests target. Each recipe's fix lives in
-- a separate migration (002_*, 003_*, ...). The buggy items are
-- appended at the bottom by later tasks; this initial section is just
-- the schema shape.
--
-- Assumes a Supabase-shaped database where these already exist:
--   * `auth.users` (table)
--   * `auth.uid()` (function)
--   * `authenticated` (role)
-- Plus the `vector` and `pgcrypto` extensions.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE sla_tier         AS ENUM ('bronze', 'silver', 'gold');
CREATE TYPE member_role      AS ENUM ('admin', 'agent', 'viewer');
CREATE TYPE ticket_status    AS ENUM ('open', 'pending', 'resolved', 'reopened');
CREATE TYPE ticket_priority  AS ENUM ('low', 'med', 'high', 'urgent');
CREATE TYPE message_author_kind AS ENUM ('customer', 'agent', 'system');
CREATE TYPE event_type       AS ENUM ('status_change', 'assignment', 'tag_added', 'tag_removed');

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE organizations (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name      TEXT NOT NULL,
    sla_tier  sla_tier NOT NULL DEFAULT 'bronze'
);

CREATE TABLE org_members (
    org_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id  UUID NOT NULL REFERENCES auth.users(id)    ON DELETE CASCADE,
    role     member_role NOT NULL,
    PRIMARY KEY (org_id, user_id)
);

CREATE TABLE customers (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL
);

CREATE TABLE tickets (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    customer_id        UUID NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    assigned_agent_id  UUID REFERENCES auth.users(id),
    status             ticket_status NOT NULL DEFAULT 'open',
    priority           ticket_priority NOT NULL DEFAULT 'med',
    subject            TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ,
    sla_due_at         TIMESTAMPTZ NOT NULL
);

CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_kind     message_author_kind NOT NULL,
    author_user_id  UUID REFERENCES auth.users(id),
    is_internal     BOOLEAN NOT NULL DEFAULT false,
    body            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ticket_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id   UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    event_type  event_type NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tags (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id  UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    UNIQUE (org_id, name)
);

CREATE TABLE ticket_tags (
    ticket_id  UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    tag_id     UUID NOT NULL REFERENCES tags(id)    ON DELETE CASCADE,
    PRIMARY KEY (ticket_id, tag_id)
);

CREATE TABLE message_embeddings (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id   UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    embedding    vector(384) NOT NULL,
    UNIQUE (message_id, chunk_index)
);

CREATE TABLE kb_articles (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    published  BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE kb_article_embeddings (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id   UUID NOT NULL REFERENCES kb_articles(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    embedding    vector(384) NOT NULL,
    UNIQUE (article_id, chunk_index)
);

-- ---------------------------------------------------------------------------
-- Grants (RLS will gate visibility once policies are added below)
-- ---------------------------------------------------------------------------

GRANT SELECT, INSERT, UPDATE, DELETE ON organizations           TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON org_members             TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON customers               TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON tickets                 TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON messages                TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON ticket_events           TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON tags                    TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON ticket_tags             TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON message_embeddings      TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON kb_articles             TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON kb_article_embeddings   TO authenticated;

-- ---------------------------------------------------------------------------
-- Buggy RPCs, policies, and triggers are appended below by recipe tasks.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Recipe 2 (correlated-rls-subqueries) — BUGGY policies on tickets
-- ---------------------------------------------------------------------------

ALTER TABLE tickets       ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_members   ENABLE ROW LEVEL SECURITY;

-- Building block: members can see their own membership row(s).
-- A naive "see all members of the same org" policy that queries
-- `org_members` from within an `org_members` policy causes infinite
-- RLS recursion. For the broader "see co-members" visibility, use a
-- SECURITY DEFINER function — see the RLS bug-classes reference page
-- for the pattern.
CREATE POLICY "members can see their own membership" ON org_members
  FOR SELECT TO authenticated
  USING (org_members.user_id = auth.uid());

-- BUG: This policy says "any authenticated user who is a member of
-- ANY org can read ALL tickets." The EXISTS subquery filters
-- `org_members.user_id = auth.uid()` but never correlates back to
-- `tickets.org_id`. Reviewers skim past it because the shape "looks
-- like other RLS policies."
CREATE POLICY "agents see org tickets" ON tickets
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM org_members
      WHERE org_members.user_id = auth.uid()
    )
  );

-- ---------------------------------------------------------------------------
-- Recipe 5 (internal-message-rls) — BUGGY policy on messages
-- ---------------------------------------------------------------------------

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

-- Prerequisite: customers must be able to read their own tickets so
-- that the messages EXISTS subquery can correlate back to the ticket.
-- This policy is correct for the customer-visibility case.
CREATE POLICY "customers see own tickets" ON tickets
  FOR SELECT TO authenticated
  USING (
    (auth.jwt() ->> 'customer_id')::uuid = tickets.customer_id
  );

-- BUG: This policy says "you can read a message iff you can read its
-- parent ticket." That's correct for agents, but customers viewing
-- their own ticket also pass it — and the policy never gates on
-- `is_internal`. Customers read internal agent triage notes meant
-- for staff only.
--
-- Customer identity is simulated via an `customer_id` claim in the
-- JWT (not an auth.users row), since customers in this app are
-- external entities tracked in the `customers` table.
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
          OR nullif(auth.jwt() ->> 'customer_id', '')::uuid = t.customer_id
        )
    )
  );

-- ---------------------------------------------------------------------------
-- Recipe 3 (idempotent-status-triggers) — BUGGY trigger on tickets
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION tg_close_sets_resolved_at()
  RETURNS TRIGGER
  LANGUAGE plpgsql
AS $$
BEGIN
  -- BUG: fires on any update where the NEW status is 'resolved',
  -- including edits that don't change the status. Editing a resolved
  -- ticket's subject bumps `resolved_at`.
  -- Uses clock_timestamp() (wall time, not transaction time) so that
  -- the second fire within the same transaction produces a distinct
  -- value — which is what a property test can observe.
  IF NEW.status = 'resolved' THEN
    NEW.resolved_at := clock_timestamp();
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER tg_close_sets_resolved_at
  BEFORE UPDATE ON tickets
  FOR EACH ROW
  EXECUTE FUNCTION tg_close_sets_resolved_at();

-- ---------------------------------------------------------------------------
-- Recipe 4 (outer-joins-and-where) — BUGGY dashboard RPC
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION organization_dashboard(p_org_id UUID)
  RETURNS TABLE (status ticket_status, count BIGINT)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  -- BUG: the LEFT JOIN intends "show every status, even zero ones",
  -- but `WHERE t.org_id = p_org_id` collapses it to INNER, dropping
  -- the zero-bucket rows. Dashboards silently lose "pending: 0",
  -- "reopened: 0", etc.
  SELECT s.status, count(t.id)
  FROM unnest(enum_range(NULL::ticket_status)) AS s(status)
  LEFT JOIN tickets t ON t.status = s.status
  WHERE t.org_id = p_org_id
  GROUP BY s.status;
$$;

GRANT EXECUTE ON FUNCTION organization_dashboard(UUID) TO authenticated;

-- ---------------------------------------------------------------------------
-- Recipe 9 (mass-assignment-without-with-check) — BUGGY UPDATE policy
-- ---------------------------------------------------------------------------

-- BUG: missing WITH CHECK. Members can `UPDATE org_members SET role
-- = 'admin' WHERE user_id = auth.uid()` and self-promote. The USING
-- clause restricts *which rows* they can touch; without WITH CHECK,
-- nothing restricts *what they can change about that row*.
CREATE POLICY "members manage their own row" ON org_members
  FOR UPDATE TO authenticated
  USING (org_members.user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Recipe 10 (missing-delete-policy) — overly permissive DELETE policy
-- ---------------------------------------------------------------------------

-- SECURITY DEFINER helper: checks whether a given user is a member of
-- a given org. Used by the co-member SELECT policy below.
--
-- Why SECURITY DEFINER: a naive subquery like
-- `EXISTS (SELECT 1 FROM org_members WHERE ...)` from inside an
-- `org_members` RLS policy causes infinite RLS recursion. The helper
-- runs as its owner (postgres), so the inner query bypasses RLS on
-- `org_members` cleanly.
--
-- Recipe 10's fix migration ships a separate helper, `is_admin_in_org`,
-- using the same pattern.
CREATE OR REPLACE FUNCTION is_member_in_org(p_org_id UUID, p_user_id UUID)
  RETURNS BOOLEAN
  LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM org_members
    WHERE org_id = p_org_id AND user_id = p_user_id
  );
$$;

GRANT EXECUTE ON FUNCTION is_member_in_org(UUID, UUID) TO authenticated;

-- Co-member visibility: members can see all other members of orgs they
-- belong to. This is the realistic SELECT policy that ships alongside the
-- DELETE policy — without it, the buggy DELETE is unobservable in raw
-- Postgres because users can only see their own row.
CREATE POLICY "members see co-members" ON org_members
  FOR SELECT TO authenticated
  USING (is_member_in_org(org_members.org_id, auth.uid()));

-- BUG: shipped as "any authenticated user can delete any membership row"
-- but the USING clause doesn't restrict *who* the deleted row belongs
-- to — a viewer can issue a DELETE that removes an admin from the org.
-- (The intent was probably user_id = auth.uid() OR is_admin_in_org(...);
-- the shipped version forgot the constraint entirely.)
CREATE POLICY "members manage their own row delete" ON org_members
  FOR DELETE TO authenticated
  USING (true);

-- ---------------------------------------------------------------------------
-- Recipe 8 (stateful-ticket-lifecycle) — BUGGY reopen RPC
-- ---------------------------------------------------------------------------

-- BUG: sets status to 'reopened' but forgets to clear resolved_at.
-- The invariant "non-resolved status -> resolved_at IS NULL" holds
-- at every isolated state — only the transition resolve->reopen
-- leaves a stale value behind.
CREATE OR REPLACE FUNCTION reopen_ticket(p_ticket_id UUID)
  RETURNS VOID
  LANGUAGE sql
  SECURITY DEFINER
  SET search_path = public
AS $$
  UPDATE tickets SET status = 'reopened' WHERE id = p_ticket_id;
$$;

GRANT EXECUTE ON FUNCTION reopen_ticket(UUID) TO authenticated;

-- ---------------------------------------------------------------------------
-- Recipe 7 (equivalent-query-optimization) — v1 reference implementation
-- ---------------------------------------------------------------------------

-- The canonical "slow but correct" version: one correlated subquery
-- per metric, joined back to `org_members` so EVERY agent in the org
-- appears in the result — including those with zero assigned tickets.
--
-- v2 (in 008_add_workload_summary_v2.sql) rewrites this as a single
-- scan with FILTER aggregations. The rewrite ships with one subtle
-- bug — see recipe doc.

CREATE OR REPLACE FUNCTION agent_workload_summary_v1(p_org_id UUID)
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
    (SELECT count(*) FROM tickets t
       WHERE t.assigned_agent_id = m.user_id
         AND t.status = 'open')    AS open_count,
    (SELECT count(*) FROM tickets t
       WHERE t.assigned_agent_id = m.user_id
         AND t.status = 'pending') AS pending_count,
    (SELECT count(*) FROM tickets t
       WHERE t.assigned_agent_id = m.user_id
         AND t.resolved_at IS NOT NULL
         AND t.sla_due_at < t.resolved_at) AS sla_breach_count
  FROM org_members m
  WHERE m.org_id = p_org_id
    AND m.role   = 'agent';
$$;

GRANT EXECUTE ON FUNCTION agent_workload_summary_v1(UUID) TO authenticated;

-- ---------------------------------------------------------------------------
-- Recipe 1 (tenant-scoped-vector-search) — BUGGY similar-ticket RPC
-- ---------------------------------------------------------------------------

-- BUG: returns the k nearest tickets by embedding distance, but never
-- filters by org_id. A ticket in org A finds matches from org B.
-- Reviewers see a sensible-looking similarity query.
CREATE OR REPLACE FUNCTION find_similar_tickets(
  p_ticket_id UUID,
  p_k INT DEFAULT 5
)
  RETURNS TABLE (ticket_id UUID, distance double precision)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  WITH target AS (
    SELECT me.embedding
    FROM message_embeddings me
    JOIN messages m ON m.id = me.message_id
    WHERE m.ticket_id = p_ticket_id
    LIMIT 1
  )
  SELECT m.ticket_id, (me.embedding <-> (SELECT embedding FROM target)) AS distance
  FROM message_embeddings me
  JOIN messages m ON m.id = me.message_id
  WHERE m.ticket_id <> p_ticket_id
  ORDER BY distance ASC
  LIMIT p_k;
$$;

GRANT EXECUTE ON FUNCTION find_similar_tickets(UUID, INT) TO authenticated;

-- ---------------------------------------------------------------------------
-- Recipe 6 (stable-vector-pagination) — BUGGY hybrid search RPC
-- ---------------------------------------------------------------------------

-- BUG: ORDER BY combined_score with no tiebreaker. When multiple
-- articles tie on score (common with short queries or sparse
-- embeddings), Postgres's tie-breaking is implementation-defined;
-- the same article can appear on two pages or vanish entirely.
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
  SELECT
    a.id AS article_id,
    (
      0.7 * (1 - (ae.embedding <=> p_query_embedding))
      + 0.3 * CASE WHEN a.title ILIKE '%' || p_text_query || '%' THEN 1.0 ELSE 0.0 END
    ) AS score
  FROM kb_articles a
  JOIN kb_article_embeddings ae ON ae.article_id = a.id
  WHERE a.org_id = p_org_id
  ORDER BY score DESC
  LIMIT p_limit OFFSET p_offset;
$$;

GRANT EXECUTE ON FUNCTION search_kb_hybrid(UUID, vector, TEXT, INT, INT) TO authenticated;
