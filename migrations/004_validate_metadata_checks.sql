-- migration: no-transaction
--
-- Second half of adding 003's CHECK constraints without downtime. VALIDATE scans the
-- existing rows holding only a SHARE UPDATE EXCLUSIVE lock, so inserts and updates
-- continue meanwhile. Running it in the same transaction as the ADD would have kept
-- 003's ACCESS EXCLUSIVE lock for the whole scan.
ALTER TABLE accounts VALIDATE CONSTRAINT accounts_metadata_shape;
ALTER TABLE transactions VALIDATE CONSTRAINT transactions_metadata_shape;
