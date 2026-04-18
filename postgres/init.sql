-- =============================================================================
-- Local AI Platform — Database Schema
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

CREATE TABLE IF NOT EXISTS conversations (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title      TEXT        NOT NULL DEFAULT 'New conversation',
    summary    TEXT        DEFAULT NULL,  -- compacted history summary
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS messages (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT        NOT NULL,
    kind            TEXT,       -- 'user' | 'assistant' | 'tool_call' | 'tool_result'
    metadata        JSONB,      -- tool name/args for tool_call, {ok,result|error} for tool_result
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
    ON messages(conversation_id, created_at);

ALTER TABLE messages ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS metadata JSONB;

-- HNSW index for fast approximate nearest-neighbour cosine search.
-- Only indexes rows that have embeddings (partial index).
CREATE INDEX IF NOT EXISTS idx_messages_embedding
    ON messages USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

-- Search sessions (each session = initial query + follow-ups)
CREATE TABLE IF NOT EXISTS search_sessions (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS search_messages (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID        NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
    role       TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT        NOT NULL,
    sources    JSONB       NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_search_messages_session_id
    ON search_messages(session_id, created_at);

-- Routines (cron-triggered agent jobs)
CREATE TABLE IF NOT EXISTS routines (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL,
    schedule    TEXT        NOT NULL,
    prompt      TEXT        NOT NULL,
    enabled     BOOLEAN     NOT NULL DEFAULT true,
    last_run_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS routine_runs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    routine_id      UUID        NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
    conversation_id UUID,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    output          TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_routine_runs_routine_id
    ON routine_runs(routine_id, started_at DESC);

-- Shared agent memory — persistent key-value store accessible by all agents.
-- Sub-agents write findings here; orchestrator reads to synthesize.
CREATE TABLE IF NOT EXISTS agent_memory (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,  -- encrypted
    agent      TEXT        DEFAULT 'main',  -- which agent wrote this
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_prefix
    ON agent_memory (key text_pattern_ops);
