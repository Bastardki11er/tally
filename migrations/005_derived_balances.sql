-- Balances and held amounts become derived data that only the database writes.
--
-- Until now ledger.py computed new balances and wrote them with UPDATE, so a stray
-- `UPDATE accounts SET balance = ...`, or a future code path that forgot to adjust
-- `held`, would silently break the books; reconciliation could only notice afterwards.
-- Now inserting an entry is what moves a balance and settling a hold is what moves the
-- held amount, so they can't disagree with the entries and holds they summarize.

-- 1. Each entry moves its account's balance and records the result as balance_after.
--    It runs once per row, in insert order, so the overdraft CHECK on accounts is
--    evaluated after every entry, not just at the end of the transaction.
CREATE FUNCTION apply_entry_to_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE accounts
       SET balance = balance + CASE WHEN NEW.direction = normal_side THEN NEW.amount ELSE -NEW.amount END
     WHERE id = NEW.account_id
    RETURNING balance INTO NEW.balance_after;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'account % does not exist', NEW.account_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER entries_apply_to_balance
    BEFORE INSERT ON entries
    FOR EACH ROW EXECUTE FUNCTION apply_entry_to_balance();

-- 2. held is the sum of the account's active holds: a new active hold adds to it, and a
--    hold leaving 'active' (captured, voided, expired) gives its amount back.
CREATE FUNCTION apply_hold_to_held() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.status = 'active' THEN
        UPDATE accounts SET held = held + NEW.amount WHERE id = NEW.account_id;
    ELSIF TG_OP = 'UPDATE' AND OLD.status = 'active' AND NEW.status <> 'active' THEN
        UPDATE accounts SET held = held - OLD.amount WHERE id = OLD.account_id;
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER holds_apply_to_held
    AFTER INSERT OR UPDATE OF status ON holds
    FOR EACH ROW EXECUTE FUNCTION apply_hold_to_held();

-- 3. Nothing else may write balance or held. pg_trigger_depth() is 1 when a client's own
--    statement fires this trigger and 2 when the UPDATE comes from one of the triggers
--    above, which is the only legitimate writer. New accounts start empty.
CREATE FUNCTION protect_derived_balances() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.balance <> 0 OR NEW.held <> 0 THEN
            RAISE EXCEPTION 'a new account starts with a zero balance'
                USING ERRCODE = 'object_not_in_prerequisite_state', HINT = 'Fund it with a transaction.';
        END IF;
    ELSIF pg_trigger_depth() < 2
          AND (NEW.balance IS DISTINCT FROM OLD.balance OR NEW.held IS DISTINCT FROM OLD.held) THEN
        RAISE EXCEPTION 'account %: balance and held are derived from entries and holds', OLD.id
            USING ERRCODE = 'object_not_in_prerequisite_state', HINT = 'Post a transaction or settle a hold instead.';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER accounts_balances_are_derived
    BEFORE INSERT OR UPDATE OF balance, held ON accounts
    FOR EACH ROW EXECUTE FUNCTION protect_derived_balances();
