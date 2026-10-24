-- Rollback for 20261024_lifecycle_up.sql. Dropping this table removes saved feedback.
BEGIN;

DROP TABLE IF EXISTS recommendation_feedback;

ALTER TABLE preference_notes DROP COLUMN IF EXISTS updated_at;
ALTER TABLE pantry_items DROP COLUMN IF EXISTS updated_at;
ALTER TABLE meal_entries DROP COLUMN IF EXISTS updated_at;

DROP INDEX IF EXISTS ix_preference_notes_active;
DROP INDEX IF EXISTS ix_decision_sessions_created_at;
DROP INDEX IF EXISTS ix_chat_messages_created_at;
DROP INDEX IF EXISTS ix_meal_entries_eaten_at;

COMMIT;
