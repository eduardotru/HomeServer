-- Initial migration for app "_template".
-- The loader runs this with search_path set to ("app__template", public),
-- so unqualified table names land in your schema.

CREATE TABLE IF NOT EXISTS items (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
