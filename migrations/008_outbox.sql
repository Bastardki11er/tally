-- A transactional outbox: every change to the books also writes an event, in the same
-- database transaction. Either both commit or neither does, so a consumer can never
-- miss a change that happened or see one that was rolled back, which is what goes wrong
-- when a service writes to its database and then, separately, to a message broker.
--
-- Clients read their events with GET /v1/events (or a relay could forward them to a
-- broker). Reading them in order without gaps takes care: ids are handed out when rows
-- are inserted, not when transactions commit, so event 101 can become visible before
-- event 100 does, and a consumer that had moved its cursor past 101 would never see 100.
-- Each event therefore records its transaction's xid8, and readers only take events from
-- transactions older than every transaction still running
-- (pg_snapshot_xmin(pg_current_snapshot())): nothing can ever commit behind that line.

CREATE TABLE outbox_events (
    id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    txid       XID8        NOT NULL DEFAULT pg_current_xact_id(),
    client_id  BIGINT      NOT NULL REFERENCES api_clients (id),
    type       TEXT        NOT NULL,
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX outbox_events_by_client ON outbox_events (client_id, txid, id);
CREATE INDEX outbox_events_by_age ON outbox_events (created_at);

-- A transaction's event carries its entries, so it is written at COMMIT (deferred),
-- once they all exist. A transaction that fails at commit takes its event with it.
CREATE FUNCTION emit_transaction_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO outbox_events (client_id, type, payload)
    SELECT NEW.client_id,
           CASE WHEN NEW.reverses_id IS NULL THEN 'transaction.posted' ELSE 'transaction.reversed' END,
           jsonb_build_object(
               'id', NEW.id, 'description', NEW.description, 'metadata', NEW.metadata,
               'reverses_id', NEW.reverses_id, 'posted_at', NEW.posted_at,
               'entries', jsonb_agg(jsonb_build_object(
                   'id', e.id, 'account_id', e.account_id, 'direction', e.direction,
                   'amount', e.amount, 'currency', e.currency, 'balance_after', e.balance_after
               ) ORDER BY e.id))
      FROM entries e
     WHERE e.transaction_id = NEW.id;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER transactions_emit_event
    AFTER INSERT ON transactions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION emit_transaction_event();

-- Accounts and holds: the row as it now is.
CREATE FUNCTION emit_row_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    type TEXT;
BEGIN
    -- Branches, not one CASE: NEW only has the columns of the table that fired.
    IF TG_TABLE_NAME = 'accounts' THEN
        type := 'account.created';
    ELSIF TG_OP = 'INSERT' THEN
        type := 'hold.created';
    ELSE
        type := 'hold.' || NEW.status; -- captured, voided, expired
    END IF;
    INSERT INTO outbox_events (client_id, type, payload) VALUES (NEW.client_id, type, to_jsonb(NEW));
    RETURN NULL;
END $$;

CREATE TRIGGER accounts_emit_event AFTER INSERT ON accounts FOR EACH ROW EXECUTE FUNCTION emit_row_event();
CREATE TRIGGER holds_emit_event AFTER INSERT OR UPDATE OF status ON holds FOR EACH ROW EXECUTE FUNCTION emit_row_event();
