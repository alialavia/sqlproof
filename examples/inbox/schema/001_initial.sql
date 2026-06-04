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
