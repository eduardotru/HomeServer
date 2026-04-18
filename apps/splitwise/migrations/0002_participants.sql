-- Adds expense participants — the people who share a given expense.
-- The payer may or may not be a participant (e.g. "A paid $10 for B only"
-- means payer=A, participants=[B]).

CREATE TABLE IF NOT EXISTS expense_participants (
    expense_id UUID NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    friend_id  UUID NOT NULL REFERENCES friends(id)  ON DELETE CASCADE,
    PRIMARY KEY (expense_id, friend_id)
);

CREATE INDEX IF NOT EXISTS expense_participants_friend_idx
    ON expense_participants(friend_id);
