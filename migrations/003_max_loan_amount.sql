-- Per-farmer eligible loan amount (AMUL-51).
-- Apply against the dedicated amul_loan DB, once per environment (dev / prod):
--   psql "$LOAN_DB_DSN" -f migrations/003_max_loan_amount.sql
-- Idempotent and additive: safe to re-run, and safe to apply while the current
-- portal and both backends are running — nothing reads the column until the new
-- builds roll out, and every existing row keeps NULL ("bank set no amount", so
-- the backends' LOAN_MAX_AMOUNT default applies).
--
-- The same DDL ships as amul-oan-api / voice-oan-api migrations/loan/002. Both
-- are ADD COLUMN IF NOT EXISTS against the one shared amul_loan database, so
-- whichever runs first wins and the other is a no-op. Do not "fix" the
-- duplication by dropping one: each repo has to be deployable on its own.

ALTER TABLE loan_eligibility_list
    ADD COLUMN IF NOT EXISTS max_loan_amount NUMERIC(12,2);
