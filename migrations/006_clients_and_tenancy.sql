-- API clients, their keys, and whose books every row belongs to.
--
-- Until now any caller could read and move any account's money. Now every request
-- authenticates with an API key, every account and transaction belongs to the key's
-- client, and a client only ever sees its own books. Like every other rule here, the
-- boundary is also enforced by the database: migration 007 adds composite foreign
-- keys that make it impossible for a transaction to touch another client's account.
--
-- Adding a NOT NULL column with a constant default only changes the catalog, so these
-- ALTERs are instant even on a large ledger. The defaults are dropped right after:
-- code that forgets to say whose row it is writing must fail, not fall back to client 1.

CREATE TABLE api_clients (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT        NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Everything recorded before clients existed belongs to this one. It has no key yet:
-- issue one with `tally create-key default`.
INSERT INTO api_clients (name) VALUES ('default');

CREATE TABLE api_keys (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id  BIGINT      NOT NULL REFERENCES api_clients (id),
    -- SHA-256 of the key. Keys are 256 random bits rather than passwords, so a fast
    -- hash is enough: there is nothing to brute-force.
    key_hash   BYTEA       NOT NULL UNIQUE,
    key_prefix TEXT        NOT NULL, -- the first characters, to tell keys apart in listings
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

ALTER TABLE accounts ADD COLUMN client_id BIGINT NOT NULL DEFAULT 1;
ALTER TABLE accounts ALTER COLUMN client_id DROP DEFAULT;
ALTER TABLE transactions ADD COLUMN client_id BIGINT NOT NULL DEFAULT 1;
ALTER TABLE transactions ALTER COLUMN client_id DROP DEFAULT;
ALTER TABLE entries ADD COLUMN client_id BIGINT NOT NULL DEFAULT 1;
ALTER TABLE entries ALTER COLUMN client_id DROP DEFAULT;
ALTER TABLE holds ADD COLUMN client_id BIGINT NOT NULL DEFAULT 1;
ALTER TABLE holds ALTER COLUMN client_id DROP DEFAULT;

-- An entry belongs to its transaction's client and a hold to its account's: the
-- database fills these in, and ignores whatever a caller wrote, so they can't be faked.
-- The composite foreign keys (007) then refuse an entry whose account belongs to
-- someone else.
CREATE FUNCTION stamp_entry_client() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT client_id INTO NEW.client_id FROM transactions WHERE id = NEW.transaction_id;
    RETURN NEW;
END $$;

CREATE TRIGGER entries_stamp_client
    BEFORE INSERT ON entries
    FOR EACH ROW EXECUTE FUNCTION stamp_entry_client();

CREATE FUNCTION stamp_hold_client() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    SELECT client_id INTO NEW.client_id FROM accounts WHERE id = NEW.account_id;
    RETURN NEW;
END $$;

CREATE TRIGGER holds_stamp_client
    BEFORE INSERT ON holds
    FOR EACH ROW EXECUTE FUNCTION stamp_hold_client();

-- An account can't change owner, for the same reason it can't change type or
-- currency: its whole history would silently move to someone else's books.
CREATE OR REPLACE FUNCTION protect_account_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.type IS DISTINCT FROM OLD.type OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.client_id IS DISTINCT FROM OLD.client_id THEN
        RAISE EXCEPTION 'account %: type, currency and owner cannot change after creation', OLD.id
            USING ERRCODE = 'object_not_in_prerequisite_state', HINT = 'Open a new account instead.';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER accounts_identity_is_fixed ON accounts;
CREATE TRIGGER accounts_identity_is_fixed
    BEFORE UPDATE OF type, currency, client_id ON accounts
    FOR EACH ROW EXECUTE FUNCTION protect_account_identity();

-- Who did what. Triggers write one row per change, inside the statement that makes it:
-- a committed change always has its record, a rolled-back one never does, no code path
-- can forget it, and it costs no extra round trip. The API names the key and request by
-- setting tally.api_key_id and tally.request_id for its transaction (set_config(..., true)
-- in the same query that checks the key). A change made any other way (psql, a script,
-- the hold-expiry job) is recorded too, with no key: exactly the rows worth a second look.
-- Append-only, like the history it describes.
CREATE TABLE audit_log (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_id   BIGINT      NOT NULL REFERENCES api_clients (id),
    api_key_id  BIGINT      REFERENCES api_keys (id), -- NULL: not made through the API
    request_id  TEXT,
    action      TEXT        NOT NULL,
    resource_id TEXT        NOT NULL
);

CREATE FUNCTION audit_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    action TEXT;
BEGIN
    -- Branches, not one CASE: NEW only has the columns of the table that fired.
    IF TG_TABLE_NAME = 'accounts' THEN
        action := 'account.created';
    ELSIF TG_TABLE_NAME = 'transactions' THEN
        action := CASE WHEN NEW.reverses_id IS NULL THEN 'transaction.posted' ELSE 'transaction.reversed' END;
    ELSIF TG_OP = 'INSERT' THEN
        action := 'hold.created';
    ELSE
        action := 'hold.' || NEW.status; -- captured, voided, expired
    END IF;
    INSERT INTO audit_log (client_id, api_key_id, request_id, action, resource_id)
    VALUES (NEW.client_id, nullif(current_setting('tally.api_key_id', true), '')::bigint,
            nullif(current_setting('tally.request_id', true), ''), action, NEW.id::text);
    RETURN NULL;
END $$;

CREATE TRIGGER accounts_audit AFTER INSERT ON accounts FOR EACH ROW EXECUTE FUNCTION audit_change();
CREATE TRIGGER transactions_audit AFTER INSERT ON transactions FOR EACH ROW EXECUTE FUNCTION audit_change();
CREATE TRIGGER holds_audit AFTER INSERT OR UPDATE OF status ON holds FOR EACH ROW EXECUTE FUNCTION audit_change();

CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_changes();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_changes();
