-- Apply once to an existing PostgreSQL database before deploying the matching code.
BEGIN;

CREATE INDEX IF NOT EXISTS ix_meal_entries_eaten_at ON meal_entries (eaten_at);
CREATE INDEX IF NOT EXISTS ix_chat_messages_created_at ON chat_messages (created_at);
CREATE INDEX IF NOT EXISTS ix_decision_sessions_created_at ON decision_sessions (created_at);
CREATE INDEX IF NOT EXISTS ix_preference_notes_active ON preference_notes (active);

ALTER TABLE meal_entries
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE pantry_items
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE preference_notes
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS recommendation_feedback (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES decision_sessions(id) ON DELETE CASCADE,
    dish_name VARCHAR(120) NOT NULL,
    action VARCHAR(16) NOT NULL CHECK (action IN ('accept', 'reject', 'eaten')),
    rating INTEGER CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_feedback_session_dish UNIQUE (session_id, dish_name)
);

COMMIT;
