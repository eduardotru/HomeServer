-- Settlements: explicit payments between friends that reduce outstanding debt.
-- Example: Alice owes Bob 50; Alice pays Bob 30 via "Settle Up" → Alice now owes Bob 20.

CREATE TABLE IF NOT EXISTS settlements (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    from_friend  UUID        NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
    to_friend    UUID        NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
    amount_cents INTEGER     NOT NULL CHECK (amount_cents > 0),
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (from_friend <> to_friend)
);

CREATE INDEX IF NOT EXISTS settlements_from_idx ON settlements(from_friend);
CREATE INDEX IF NOT EXISTS settlements_to_idx   ON settlements(to_friend);
