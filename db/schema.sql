-- Mobile Money platform schema (Aurora PostgreSQL 16 compatible).
-- Money is stored as BIGINT minor units (ngwee for ZMW). Never floats.
--
-- Core invariants enforced by the database itself (not only by application code):
--   1. A wallet balance can never go negative (except the MINT system wallet).
--   2. ledger_entries is append-only: UPDATE / DELETE / TRUNCATE raise errors.
--   3. A saga step can write at most one DEBIT and one CREDIT entry (replay-safe).
--   4. Every wallet has its own hash chain over its ledger entries (tamper evidence).
--   5. SUM(wallets.balance) == 0 at all times (MINT goes negative by total e-money issued).


-- ---------------------------------------------------------------- wallets
CREATE TABLE IF NOT EXISTS wallets (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        text        NOT NULL,                 -- Cognito 'sub' (or 'system')
    kind            text        NOT NULL CHECK (kind IN ('CUSTOMER','MERCHANT','AGENT','SYSTEM')),
    currency        char(3)     NOT NULL DEFAULT 'ZMW',
    balance         bigint      NOT NULL DEFAULT 0,
    allow_negative  boolean     NOT NULL DEFAULT false,
    status          text        NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','FROZEN','CLOSED')),
    entry_seq       bigint      NOT NULL DEFAULT 0,       -- last ledger sequence for this wallet
    last_entry_hash text        NOT NULL DEFAULT repeat('0', 64),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT balance_non_negative CHECK (balance >= 0 OR allow_negative)
);
CREATE INDEX IF NOT EXISTS wallets_owner_idx ON wallets (owner_id);

-- ---------------------------------------------------------------- transactions
CREATE TABLE IF NOT EXISTS transactions (
    id              uuid PRIMARY KEY,
    type            text        NOT NULL CHECK (type IN
                      ('TRANSFER','MERCHANT_PAYMENT','CASH_IN','CASH_OUT','FLOAT_TOPUP','REVERSAL')),
    status          text        NOT NULL CHECK (status IN
                      ('PENDING','COMPLETED','REJECTED','FAILED','REVERSED')),
    from_wallet     uuid        NOT NULL REFERENCES wallets(id),
    to_wallet       uuid        NOT NULL REFERENCES wallets(id),
    amount          bigint      NOT NULL CHECK (amount > 0),
    currency        char(3)     NOT NULL,
    reverses_txn_id uuid        REFERENCES transactions(id),
    reason          text,
    geo_lat         double precision,
    geo_lon         double precision,
    correlation_id  text,
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS txn_from_idx    ON transactions (from_wallet, created_at DESC);
CREATE INDEX IF NOT EXISTS txn_created_idx ON transactions (created_at);
-- A transaction can have at most one live (pending/completed) reversal; failed attempts don't block.
CREATE UNIQUE INDEX IF NOT EXISTS txn_one_reversal_idx
    ON transactions (reverses_txn_id)
    WHERE reverses_txn_id IS NOT NULL AND status IN ('PENDING','COMPLETED');

-- ---------------------------------------------------------------- ledger (append-only)
CREATE TABLE IF NOT EXISTS ledger_entries (
    id              bigserial   PRIMARY KEY,
    txn_id          uuid        NOT NULL,
    step            text        NOT NULL,                 -- saga step, e.g. 'reserve','credit'
    wallet_id       uuid        NOT NULL REFERENCES wallets(id),
    wallet_seq      bigint      NOT NULL,                 -- position in the wallet's hash chain
    direction       text        NOT NULL CHECK (direction IN ('DEBIT','CREDIT')),
    amount          bigint      NOT NULL CHECK (amount > 0),
    balance_after   bigint      NOT NULL,
    entry_type      text        NOT NULL,
    prev_hash       text        NOT NULL,
    entry_hash      text        NOT NULL,
    correlation_id  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (wallet_id, wallet_seq),
    UNIQUE (txn_id, step, direction)                      -- replay of a step cannot double-post
);
CREATE INDEX IF NOT EXISTS ledger_txn_idx    ON ledger_entries (txn_id);
CREATE INDEX IF NOT EXISTS ledger_wallet_idx ON ledger_entries (wallet_id, wallet_seq);

CREATE OR REPLACE FUNCTION forbid_ledger_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only (% blocked)', TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ledger_no_update_delete ON ledger_entries;
CREATE TRIGGER ledger_no_update_delete
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();

DROP TRIGGER IF EXISTS ledger_no_truncate ON ledger_entries;
CREATE TRIGGER ledger_no_truncate
    BEFORE TRUNCATE ON ledger_entries
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_ledger_mutation();

-- ---------------------------------------------------------------- idempotency
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope           text        NOT NULL,                 -- caller identity + operation
    key             text        NOT NULL,
    request_hash    text        NOT NULL,                 -- same key + different body => 422
    status          text        NOT NULL CHECK (status IN ('IN_PROGRESS','COMPLETED')),
    response_code   integer,
    response_body   jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    PRIMARY KEY (scope, key)
);

-- ---------------------------------------------------------------- saga state
CREATE TABLE IF NOT EXISTS sagas (
    txn_id          uuid PRIMARY KEY REFERENCES transactions(id),
    state           text        NOT NULL,                 -- see momo.saga.orchestrator.SagaState
    completed_steps text[]      NOT NULL DEFAULT '{}',
    last_error      text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- disputes
CREATE TABLE IF NOT EXISTS disputes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    txn_id          uuid        NOT NULL REFERENCES transactions(id),
    raised_by       text        NOT NULL,
    reason          text        NOT NULL,
    status          text        NOT NULL DEFAULT 'OPEN'
                      CHECK (status IN ('OPEN','RESOLVED_REVERSED','RESOLVED_REJECTED')),
    resolution_note text,
    resolved_by     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS disputes_one_open_idx ON disputes (txn_id) WHERE status = 'OPEN';

-- ---------------------------------------------------------------- fraud
CREATE TABLE IF NOT EXISTS fraud_alerts (
    id              bigserial PRIMARY KEY,
    txn_id          uuid        NOT NULL,
    wallet_id       uuid        NOT NULL,
    decision        text        NOT NULL,
    rules           jsonb       NOT NULL,
    correlation_id  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (txn_id, decision)                             -- SQS is at-least-once: dedupe consumers
);

-- ---------------------------------------------------------------- transactional outbox
CREATE TABLE IF NOT EXISTS outbox_events (
    id              bigserial PRIMARY KEY,
    event_type      text        NOT NULL,
    txn_id          uuid        NOT NULL,
    payload         jsonb       NOT NULL,
    correlation_id  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz,
    UNIQUE (event_type, txn_id)
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON outbox_events (id) WHERE published_at IS NULL;

-- ---------------------------------------------------------------- settlement reports
CREATE TABLE IF NOT EXISTS settlement_reports (
    id              bigserial PRIMARY KEY,
    report_date     date        NOT NULL,
    currency        char(3)     NOT NULL,
    summary         jsonb       NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (report_date, currency)
);

-- ---------------------------------------------------------------- bootstrap system wallets
-- MINT: the only wallet allowed to go negative. Issuing e-money = MINT -> AGENT float.
-- ESCROW: holds funds between saga 'reserve' and 'credit'.
INSERT INTO wallets (id, owner_id, kind, currency, allow_negative)
VALUES ('00000000-0000-0000-0000-000000000001', 'system', 'SYSTEM', 'ZMW', true)
ON CONFLICT (id) DO NOTHING;
INSERT INTO wallets (id, owner_id, kind, currency, allow_negative)
VALUES ('00000000-0000-0000-0000-000000000002', 'system', 'SYSTEM', 'ZMW', false)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------- application role (least privilege)
-- Created by infra/terraform (db_init Lambda) in AWS; shown here for documentation and local use.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'momo_app') THEN
        CREATE ROLE momo_app NOLOGIN;
    END IF;
END $$;
GRANT SELECT, INSERT, UPDATE ON wallets, transactions, sagas, idempotency_keys,
      fraud_alerts, outbox_events, settlement_reports, disputes TO momo_app;
GRANT SELECT, INSERT ON ledger_entries TO momo_app;      -- no UPDATE / DELETE / TRUNCATE
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO momo_app;
REVOKE UPDATE, DELETE, TRUNCATE ON ledger_entries FROM momo_app;
