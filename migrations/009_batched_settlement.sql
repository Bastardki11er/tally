-- Paying a hot account without queueing on its row lock.
--
-- Every transfer locks both accounts' rows, so when thousands of customers pay one
-- merchant, every payment waits for that one row: about 1,500 transfers/s at best, with
-- seconds of tail latency. A hold with settle_to_account_id is a payment that only
-- touches the payer's row now (the money is reserved, so it can't be spent twice) and
-- is settled later together with others: one transaction debits up to a hundred payers
-- and credits the merchant once. That's how card payments work (authorize now, clear in
-- batches): the payer's side is immediate, the merchant's balance trails by a few seconds.

ALTER TABLE holds ADD COLUMN settle_to_account_id BIGINT;

-- The destination belongs to the same client as the hold. The column is new and empty,
-- so validating this key only reads the holds table, it never waits on anything.
ALTER TABLE holds ADD CONSTRAINT holds_settle_to_client
    FOREIGN KEY (client_id, settle_to_account_id) REFERENCES accounts (client_id, id);

-- The settlement queue: active payments, oldest first. Partial, so it only ever holds
-- the few payments waiting for the next batch.
CREATE INDEX holds_awaiting_settlement ON holds (created_at)
    WHERE status = 'active' AND settle_to_account_id IS NOT NULL;

-- Where a payment goes is fixed when it's made, like its amount and account.
CREATE OR REPLACE FUNCTION enforce_hold_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'active' THEN
        RAISE EXCEPTION 'hold % is already %; settled holds cannot change', OLD.id, OLD.status
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
    IF NEW.account_id <> OLD.account_id OR NEW.amount <> OLD.amount OR NEW.created_at <> OLD.created_at
       OR NEW.settle_to_account_id IS DISTINCT FROM OLD.settle_to_account_id THEN
        RAISE EXCEPTION 'hold %: account, amount, destination and creation time cannot change', OLD.id
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
    NEW.updated_at := now();  -- maintained here, so no caller can forget it
    RETURN NEW;
END $$;
