-- Initial migration for app "splitwise".
-- Runs once, tracked in _meta.app_migrations. The loader sets search_path to
-- ("app_splitwise", public), so unqualified table names land in the app schema.

CREATE TABLE IF NOT EXISTS friends (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS expenses (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    amount_cents INTEGER     NOT NULL CHECK (amount_cents > 0),
    description  TEXT        NOT NULL,
    payer_id     UUID        REFERENCES friends(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS expenses_payer_idx ON expenses(payer_id);
CREATE INDEX IF NOT EXISTS expenses_created_idx ON expenses(created_at DESC);
