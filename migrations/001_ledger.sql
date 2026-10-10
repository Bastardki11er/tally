-- Tally ledger schema.
--
-- Every rule that keeps the books correct is enforced here, in the database,
-- so that no application bug, script or manual query can break it:
--
--   * every transaction balances: debits = credits, per currency (deferred trigger)
--   * a transaction has at least two entries (deferred trigger)
--   * entries and transactions are append-only (triggers); mistakes are reversed, never edited
--   * an entry's currency matches its account's currency (composite foreign key)
--   * accounts that may not go negative never do (CHECK constraint)
--   * a transaction can be reversed at most once (UNIQUE)

CREATE TABLE accounts (
    id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    type           TEXT        NOT NULL CHECK (type IN ('asset', 'liability', 'equity', 'revenue', 'expense')),
    -- Asset and expense accounts grow with debits; the others grow with credits.
    normal_side    TEXT        NOT NULL GENERATED ALWAYS AS (
                                   CASE WHEN type IN ('asset', 'expense') THEN 'debit' ELSE 'credit' END
                               ) STORED,
    currency       CHAR(3)     NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    allow_negative BOOLEAN     NOT NULL DEFAULT false,
    -- Posted balance in minor units (cents), measured on the account's normal side.
    balance        BIGINT      NOT NULL DEFAULT 0,
    -- Funds reserved by active holds: not spent yet, but no longer spendable.
    held           BIGINT      NOT NULL DEFAULT 0 CHECK (held >= 0),
    available      BIGINT      NOT NULL GENERATED ALWAYS AS (balance - held) STORED,
    metadata       JSONB       NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Last line of defence against overdrafts, whatever the application does.
    CONSTRAINT accounts_no_overdraft CHECK (allow_negative OR balance - held >= 0),
    -- Target of the composite foreign key that ties each entry's currency to its account.
    CONSTRAINT accounts_id_currency UNIQUE (id, currency)
);

CREATE TABLE transactions (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    description TEXT        NOT NULL DEFAULT '' CHECK (length(description) <= 500),
    metadata    JSONB       NOT NULL DEFAULT '{}',
    -- Set on a reversal; UNIQUE means a transaction can be reversed at most once.
    reverses_id UUID        UNIQUE REFERENCES transactions (id),
    posted_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE entries (
    id             BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id UUID        NOT NULL REFERENCES transactions (id),
    account_id     BIGINT      NOT NULL,
    direction      TEXT        NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount         BIGINT      NOT NULL CHECK (amount > 0),
    currency       CHAR(3)     NOT NULL,
    -- The account's balance right after this entry. Gives statements a running
    -- balance and answers "balance as of T" without re-summing history.
    balance_after  BIGINT      NOT NULL,
    -- clock_timestamp() rather than now(): it is taken when the row is inserted,
    -- after the account row lock is held, so per-account timestamps are ordered
    -- exactly like the entries themselves.
    posted_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT entries_account_currency FOREIGN KEY (account_id, currency) REFERENCES accounts (id, currency)
);

-- Account statements and cursor pagination walk entries of one account by id.
CREATE INDEX entries_by_account ON entries (account_id, id);
CREATE INDEX entries_by_transaction ON entries (transaction_id);

CREATE TABLE holds (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id              BIGINT      NOT NULL REFERENCES accounts (id),
    amount                  BIGINT      NOT NULL CHECK (amount > 0),
    status                  TEXT        NOT NULL DEFAULT 'active'
                                        CHECK (status IN ('active', 'captured', 'voided', 'expired')),
    description             TEXT        NOT NULL DEFAULT '' CHECK (length(description) <= 500),
    expires_at              TIMESTAMPTZ NOT NULL,
    captured_transaction_id UUID        REFERENCES transactions (id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT holds_captured_has_transaction
        CHECK ((status = 'captured') = (captured_transaction_id IS NOT NULL))
);

CREATE INDEX holds_by_account ON holds (account_id);
-- The expiry job only ever scans active holds; a partial index keeps that scan tiny.
CREATE INDEX holds_active_by_expiry ON holds (expires_at) WHERE status = 'active';

CREATE TABLE idempotency_keys (
    key          TEXT        PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 300),
    request_hash TEXT        NOT NULL,
    status_code  INT,
    response     JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idempotency_keys_by_age ON idempotency_keys (created_at);

-- Debits must equal credits within each currency. DEFERRABLE INITIALLY DEFERRED:
-- the check runs at COMMIT, once all of the transaction's entries exist.
CREATE FUNCTION assert_entries_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    unbalanced RECORD;
BEGIN
    SELECT currency, sum(CASE direction WHEN 'debit' THEN amount ELSE -amount END) AS net
      INTO unbalanced
      FROM entries
     WHERE transaction_id = NEW.transaction_id
     GROUP BY currency
    HAVING sum(CASE direction WHEN 'debit' THEN amount ELSE -amount END) <> 0
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'transaction % does not balance in %: debits - credits = %',
            NEW.transaction_id, unbalanced.currency, unbalanced.net
            USING ERRCODE = 'integrity_constraint_violation', CONSTRAINT = 'entries_balanced';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER entries_balanced
    AFTER INSERT ON entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entries_balanced();

CREATE FUNCTION assert_transaction_has_entries() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM entries WHERE transaction_id = NEW.id) < 2 THEN
        RAISE EXCEPTION 'transaction % needs at least two entries', NEW.id
            USING ERRCODE = 'integrity_constraint_violation', CONSTRAINT = 'transactions_have_entries';
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER transactions_have_entries
    AFTER INSERT ON transactions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_transaction_has_entries();

-- History is immutable. A wrong transaction is corrected with a reversal.
CREATE FUNCTION forbid_changes() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed, post a reversal instead', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'object_not_in_prerequisite_state';
END $$;

CREATE TRIGGER entries_append_only
    BEFORE UPDATE OR DELETE ON entries
    FOR EACH ROW EXECUTE FUNCTION forbid_changes();

CREATE TRIGGER transactions_append_only
    BEFORE UPDATE OR DELETE ON transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_changes();
