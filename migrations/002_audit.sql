-- Loan-portal redemption audit log (design doc §4).
-- Append-only. Doubles as the rate-limit signal source (count recent
-- code lookups per session/IP) and the reconciliation record.
-- Apply against the dedicated amul_loan Postgres:
--   psql "$LOAN_DB_DSN" -f migrations/002_audit.sql
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS loan_redemption_audit (
    id           BIGSERIAL PRIMARY KEY,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor        VARCHAR(128) NOT NULL,     -- 'portal:shared' now; username later
    action       VARCHAR(16)  NOT NULL,     -- 'verify' | 'redeem' | 'redeem_noop'
    lookup_kind  VARCHAR(8),                -- 'phone' | 'code'
    code         VARCHAR(12),               -- code acted on (nullable on miss)
    phone        VARCHAR(15),
    result       VARCHAR(24)  NOT NULL,     -- 'ok'|'not_found'|'already'|'expired'|'cancelled'|'rate_limited'
    src_ip       VARCHAR(64),
    detail       JSONB
);

CREATE INDEX IF NOT EXISTS ix_audit_at ON loan_redemption_audit (at);
CREATE INDEX IF NOT EXISTS ix_audit_ip ON loan_redemption_audit (src_ip, at);
