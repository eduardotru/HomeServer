-- =============================================================================
-- Local AI Platform — Database Schema
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

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
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
    ON messages(conversation_id, created_at);
