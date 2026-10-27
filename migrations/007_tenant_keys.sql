-- migration: no-transaction
--
-- The database half of tenant isolation, built without blocking writes on a big ledger:
-- indexes CONCURRENTLY, and foreign keys added NOT VALID (instant) and then validated
-- (a scan that holds only a SHARE UPDATE EXCLUSIVE lock, so inserts carry on).
-- Everything is dropped IF EXISTS before it's created, so a run that failed halfway can
-- simply be repeated.

-- Constraints first: the indexes below can't be dropped while foreign keys depend on them.
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS accounts_client;
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_client;
ALTER TABLE entries DROP CONSTRAINT IF EXISTS entries_account_client;
ALTER TABLE entries DROP CONSTRAINT IF EXISTS entries_transaction_client;
ALTER TABLE holds DROP CONSTRAINT IF EXISTS holds_account_client;

-- Targets for the composite foreign keys. (client_id, id) also serves "list a client's
-- accounts in id order", so accounts needs no other index for it.
DROP INDEX CONCURRENTLY IF EXISTS accounts_client_id_key;
CREATE UNIQUE INDEX CONCURRENTLY accounts_client_id_key ON accounts (client_id, id);
DROP INDEX CONCURRENTLY IF EXISTS transactions_client_id_key;
CREATE UNIQUE INDEX CONCURRENTLY transactions_client_id_key ON transactions (client_id, id);

-- The activity feed is now always one client's: this replaces the global (posted_at, id).
DROP INDEX CONCURRENTLY IF EXISTS transactions_by_client_time;
CREATE INDEX CONCURRENTLY transactions_by_client_time ON transactions (client_id, posted_at, id);
DROP INDEX CONCURRENTLY IF EXISTS transactions_by_posted_at;

ALTER TABLE accounts ADD CONSTRAINT accounts_client FOREIGN KEY (client_id) REFERENCES api_clients (id) NOT VALID;
ALTER TABLE transactions ADD CONSTRAINT transactions_client
    FOREIGN KEY (client_id) REFERENCES api_clients (id) NOT VALID;

-- An entry's account and its transaction belong to the same client, and so do a hold
-- and its account: a transaction can't touch another client's money, whatever the code does.
ALTER TABLE entries ADD CONSTRAINT entries_account_client
    FOREIGN KEY (client_id, account_id) REFERENCES accounts (client_id, id) NOT VALID;
ALTER TABLE entries ADD CONSTRAINT entries_transaction_client
    FOREIGN KEY (client_id, transaction_id) REFERENCES transactions (client_id, id) NOT VALID;
ALTER TABLE holds ADD CONSTRAINT holds_account_client
    FOREIGN KEY (client_id, account_id) REFERENCES accounts (client_id, id) NOT VALID;

ALTER TABLE accounts VALIDATE CONSTRAINT accounts_client;
ALTER TABLE transactions VALIDATE CONSTRAINT transactions_client;
ALTER TABLE entries VALIDATE CONSTRAINT entries_account_client;
ALTER TABLE entries VALIDATE CONSTRAINT entries_transaction_client;
ALTER TABLE holds VALIDATE CONSTRAINT holds_account_client;
