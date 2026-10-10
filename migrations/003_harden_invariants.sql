-- Closes gaps found while reviewing 001. Each rule below used to be respected only
-- because the application happened to behave; now the database enforces it.

-- 1. TRUNCATE skips row-level triggers, so it slipped past 001's append-only rule:
--    `TRUNCATE entries` would have erased the history in one statement. Statement-level
--    BEFORE TRUNCATE triggers also fire for tables reached through CASCADE.
CREATE TRIGGER entries_no_truncate
    BEFORE TRUNCATE ON entries
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_changes();

CREATE TRIGGER transactions_no_truncate
    BEFORE TRUNCATE ON transactions
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_changes();

-- 2. An account's type decides what its balance means: `normal_side` is derived from
--    it, so changing an asset into a liability would silently flip the sign of its
--    whole history. Currency is pinned to existing entries by a foreign key, but an
--    account without entries could still have switched. Neither may change.
CREATE FUNCTION protect_account_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.type IS DISTINCT FROM OLD.type OR NEW.currency IS DISTINCT FROM OLD.currency THEN
        RAISE EXCEPTION 'account %: type and currency cannot change after creation', OLD.id
            USING ERRCODE = 'object_not_in_prerequisite_state', HINT = 'Open a new account instead.';
    END IF;
    RETURN NEW;
END $$;

-- UPDATE OF: fires only when those columns are in the SET list, so the frequent
-- balance updates pay nothing for it.
CREATE TRIGGER accounts_identity_is_fixed
    BEFORE UPDATE OF type, currency ON accounts
    FOR EACH ROW EXECUTE FUNCTION protect_account_identity();

-- 3. Holds follow a one-way lifecycle: active -> captured | voided | expired. Moving a
--    settled hold back to active, or editing its amount, would desynchronize
--    accounts.held from the holds that are really active. Deleting one would too.
CREATE FUNCTION enforce_hold_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'active' THEN
        RAISE EXCEPTION 'hold % is already %; settled holds cannot change', OLD.id, OLD.status
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
    IF NEW.account_id <> OLD.account_id OR NEW.amount <> OLD.amount OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'hold %: account, amount and creation time cannot change', OLD.id
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
    NEW.updated_at := now();  -- maintained here, so no caller can forget it
    RETURN NEW;
END $$;

CREATE TRIGGER holds_lifecycle
    BEFORE UPDATE ON holds
    FOR EACH ROW EXECUTE FUNCTION enforce_hold_lifecycle();

CREATE FUNCTION forbid_hold_removal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'holds are permanent records: % is not allowed', TG_OP
        USING ERRCODE = 'object_not_in_prerequisite_state', HINT = 'Void the hold instead.';
END $$;

CREATE TRIGGER holds_no_delete
    BEFORE DELETE ON holds
    FOR EACH ROW EXECUTE FUNCTION forbid_hold_removal();

CREATE TRIGGER holds_no_truncate
    BEFORE TRUNCATE ON holds
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_hold_removal();

-- 4. "Balance as of T" (and the index from 002) relies on posted_at rising with id
--    inside each account. 001 only got that from a column DEFAULT, which an INSERT can
--    override, and from clock_timestamp(), which goes backwards if the server clock
--    is stepped back. The database now stamps the time itself and never lets it go
--    below the account's previous entry (ties are ordered by id). Callers hold the
--    account's row lock while inserting, so the lookup can't race.
CREATE FUNCTION stamp_entry_time() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.posted_at := greatest(
        clock_timestamp(),
        (SELECT e.posted_at FROM entries e
          WHERE e.account_id = NEW.account_id
          ORDER BY e.posted_at DESC, e.id DESC
          LIMIT 1)
    );
    RETURN NEW;
END $$;

CREATE TRIGGER entries_stamp_time
    BEFORE INSERT ON entries
    FOR EACH ROW EXECUTE FUNCTION stamp_entry_time();

-- 5. metadata was any JSON value of any size: an array, or a 50 MB blob. It must now
--    be a JSON object of at most 4 KB. NOT VALID adds the rule for new rows at once
--    with only a brief lock; checking the existing rows happens in 004, outside this
--    transaction, so a big table keeps taking writes while it is scanned.
ALTER TABLE accounts ADD CONSTRAINT accounts_metadata_shape
    CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 4096) NOT VALID;

ALTER TABLE transactions ADD CONSTRAINT transactions_metadata_shape
    CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 4096) NOT VALID;
