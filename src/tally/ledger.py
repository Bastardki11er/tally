"""The ledger: accounts, balanced transactions, holds and reversals.

Every method takes an open connection and runs inside the caller's database
transaction, so the API layer can combine an operation with its idempotency
record atomically.

Concurrency: every operation locks the account rows it touches with
``SELECT ... FOR UPDATE``, always in ascending id order. Two transfers between
the same accounts in opposite directions therefore queue up instead of
deadlocking, and balances are read and written under the same lock, so no
update is ever lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.types.json import Jsonb

from .errors import conflict, not_found, unprocessable

Direction = Literal["debit", "credit"]
AccountType = Literal["asset", "liability", "equity", "revenue", "expense"]

ACCOUNT_COLUMNS = (
    "id, name, type, normal_side, currency, allow_negative, balance, held, available, metadata, created_at"
)
TRANSACTION_COLUMNS = "id, description, metadata, reverses_id, posted_at"
ENTRY_COLUMNS = "id, account_id, direction, amount, currency, balance_after, posted_at"
HOLD_COLUMNS = (
    "id, account_id, amount, status, description, expires_at, captured_transaction_id, created_at, updated_at"
)


@dataclass(frozen=True)
class Leg:
    """One side of a transaction: move ``amount`` minor units on ``account_id``."""

    account_id: int
    direction: Direction
    amount: int


def opposite(direction: str) -> Direction:
    return "credit" if direction == "debit" else "debit"


class Ledger:
    # --- accounts -----------------------------------------------------------------

    def create_account(
        self,
        conn: Connection,
        name: str,
        type: AccountType,
        currency: str,
        allow_negative: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        return conn.execute(
            f"INSERT INTO accounts (name, type, currency, allow_negative, metadata) VALUES (%s, %s, %s, %s, %s) "
            f"RETURNING {ACCOUNT_COLUMNS}",
            (name, type, currency, allow_negative, Jsonb(metadata or {})),
        ).fetchone()

    def get_account(self, conn: Connection, account_id: int) -> dict:
        account = conn.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id = %s", (account_id,)).fetchone()
        if account is None:
            raise not_found("ACCOUNT_NOT_FOUND", f"Account {account_id} does not exist")
        return account

    def list_accounts(self, conn: Connection, limit: int = 50, after_id: int | None = None) -> list[dict]:
        return conn.execute(
            f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id > %s ORDER BY id LIMIT %s",
            (after_id or 0, limit),
        ).fetchall()

    # --- posting ------------------------------------------------------------------

    def post(
        self,
        conn: Connection,
        legs: list[Leg],
        description: str = "",
        metadata: dict | None = None,
        reverses_id: UUID | None = None,
    ) -> dict:
        """Posts a balanced transaction and updates every affected balance."""
        if len(legs) < 2:
            raise unprocessable("TOO_FEW_ENTRIES", "A transaction needs at least two entries")
        accounts = self._lock_accounts(conn, {leg.account_id for leg in legs})

        net: dict[str, int] = {}
        for leg in legs:
            currency = accounts[leg.account_id]["currency"]
            net[currency] = net.get(currency, 0) + (leg.amount if leg.direction == "debit" else -leg.amount)
        unbalanced = {currency: amount for currency, amount in net.items() if amount != 0}
        if unbalanced:
            detail = ", ".join(f"{c} debits - credits = {n}" for c, n in unbalanced.items())
            raise unprocessable("UNBALANCED", f"Debits must equal credits in every currency ({detail})")

        # Apply the legs in order, recording each account's running balance.
        balances = {account_id: account["balance"] for account_id, account in accounts.items()}
        balance_after: list[int] = []
        for leg in legs:
            account = accounts[leg.account_id]
            balances[leg.account_id] += leg.amount if leg.direction == account["normal_side"] else -leg.amount
            balance_after.append(balances[leg.account_id])

        for account_id, balance in balances.items():
            account = accounts[account_id]
            if not account["allow_negative"] and balance - account["held"] < 0:
                raise conflict(
                    "INSUFFICIENT_FUNDS",
                    f"Account {account_id} has {account['available']} available, "
                    f"this transaction needs {account['balance'] - balance}",
                )

        transaction = conn.execute(
            f"INSERT INTO transactions (description, metadata, reverses_id) VALUES (%s, %s, %s) "
            f"RETURNING {TRANSACTION_COLUMNS}",
            (description, Jsonb(metadata or {}), reverses_id),
        ).fetchone()
        entries = conn.execute(
            f"""
            INSERT INTO entries (transaction_id, account_id, direction, amount, currency, balance_after)
            SELECT %s, * FROM unnest(%s::bigint[], %s::text[], %s::bigint[], %s::char(3)[], %s::bigint[])
            RETURNING {ENTRY_COLUMNS}
            """,
            (
                transaction["id"],
                [leg.account_id for leg in legs],
                [leg.direction for leg in legs],
                [leg.amount for leg in legs],
                [accounts[leg.account_id]["currency"] for leg in legs],
                balance_after,
            ),
        ).fetchall()
        conn.execute(
            "UPDATE accounts AS a SET balance = v.balance "
            "FROM unnest(%s::bigint[], %s::bigint[]) AS v (id, balance) WHERE a.id = v.id",
            (list(balances), list(balances.values())),
        )
        return {**transaction, "entries": sorted(entries, key=lambda e: e["id"])}

    def transfer(
        self,
        conn: Connection,
        debit_account_id: int,
        credit_account_id: int,
        amount: int,
        description: str = "",
        metadata: dict | None = None,
    ) -> dict:
        """The common two-entry case: debit one account, credit another."""
        if debit_account_id == credit_account_id:
            raise unprocessable("SAME_ACCOUNT", "Debit and credit accounts must differ")
        legs = [Leg(debit_account_id, "debit", amount), Leg(credit_account_id, "credit", amount)]
        return self.post(conn, legs, description, metadata)

    def reverse(self, conn: Connection, transaction_id: UUID, description: str = "") -> dict:
        """Undoes a transaction by posting its mirror image. History is never edited."""
        # Row-lock the original first, so concurrent reversals of the same transaction
        # queue up here. Without it, the losers would pass the "already reversed?" check,
        # wait on the account locks, and then fail with a misleading INSUFFICIENT_FUNDS
        # (the money having just been moved back) instead of ALREADY_REVERSED.
        original = conn.execute(
            "SELECT id, reverses_id FROM transactions WHERE id = %s FOR UPDATE", (transaction_id,)
        ).fetchone()
        if original is None:
            raise not_found("TRANSACTION_NOT_FOUND", f"Transaction {transaction_id} does not exist")
        if original["reverses_id"] is not None:
            raise conflict("CANNOT_REVERSE_REVERSAL", "A reversal cannot itself be reversed")
        if conn.execute("SELECT 1 FROM transactions WHERE reverses_id = %s", (transaction_id,)).fetchone():
            raise conflict("ALREADY_REVERSED", f"Transaction {transaction_id} has already been reversed")

        rows = conn.execute(
            "SELECT account_id, direction, amount FROM entries WHERE transaction_id = %s ORDER BY id",
            (transaction_id,),
        ).fetchall()
        legs = [Leg(r["account_id"], opposite(r["direction"]), r["amount"]) for r in rows]
        try:
            return self.post(conn, legs, description or f"Reversal of {transaction_id}", reverses_id=transaction_id)
        except psycopg.errors.UniqueViolation as exc:  # a concurrent reversal won the race
            raise conflict("ALREADY_REVERSED", f"Transaction {transaction_id} has already been reversed") from exc

    # --- holds --------------------------------------------------------------------

    def create_hold(
        self, conn: Connection, account_id: int, amount: int, expires_at: datetime, description: str = ""
    ) -> dict:
        """Reserves funds for a later capture: they stay in the balance but are no longer available."""
        account = self._lock_accounts(conn, {account_id})[account_id]
        if not account["allow_negative"] and account["available"] < amount:
            raise conflict(
                "INSUFFICIENT_FUNDS", f"Account {account_id} has {account['available']} available, hold needs {amount}"
            )
        hold = conn.execute(
            f"INSERT INTO holds (account_id, amount, expires_at, description) VALUES (%s, %s, %s, %s) "
            f"RETURNING {HOLD_COLUMNS}",
            (account_id, amount, expires_at, description),
        ).fetchone()
        conn.execute("UPDATE accounts SET held = held + %s WHERE id = %s", (amount, account_id))
        return hold

    def capture_hold(
        self,
        conn: Connection,
        hold_id: UUID,
        destination_account_id: int,
        amount: int | None = None,
        description: str = "",
    ) -> dict:
        """Turns a hold into a real transaction. Any uncaptured remainder is released."""
        hold = self._lock_active_hold(conn, hold_id)
        amount = hold["amount"] if amount is None else amount
        if amount > hold["amount"]:
            raise unprocessable("CAPTURE_EXCEEDS_HOLD", f"Cannot capture {amount}, the hold is for {hold['amount']}")
        if destination_account_id == hold["account_id"]:
            raise unprocessable("SAME_ACCOUNT", "Destination must differ from the held account")

        accounts = self._lock_accounts(conn, {hold["account_id"], destination_account_id})
        # Release the reservation first, so the spend below is checked against real
        # funds rather than counting the held amount twice.
        conn.execute("UPDATE accounts SET held = held - %s WHERE id = %s", (hold["amount"], hold["account_id"]))
        # Spending moves the held account away from its normal side, whatever its type.
        out = opposite(accounts[hold["account_id"]]["normal_side"])
        legs = [Leg(hold["account_id"], out, amount), Leg(destination_account_id, opposite(out), amount)]
        transaction = self.post(conn, legs, description or hold["description"], {"hold_id": str(hold_id)})
        captured = conn.execute(
            f"UPDATE holds SET status = 'captured', captured_transaction_id = %s, updated_at = now() "
            f"WHERE id = %s RETURNING {HOLD_COLUMNS}",
            (transaction["id"], hold_id),
        ).fetchone()
        return {"hold": captured, "transaction": transaction}

    def void_hold(self, conn: Connection, hold_id: UUID) -> dict:
        hold = self._lock_active_hold(conn, hold_id)
        self._lock_accounts(conn, {hold["account_id"]})
        conn.execute("UPDATE accounts SET held = held - %s WHERE id = %s", (hold["amount"], hold["account_id"]))
        return conn.execute(
            f"UPDATE holds SET status = 'voided', updated_at = now() WHERE id = %s RETURNING {HOLD_COLUMNS}",
            (hold_id,),
        ).fetchone()

    def expire_holds(self, conn: Connection, limit: int = 500) -> int:
        """Releases holds whose expiry has passed. Safe to run on several instances at once."""
        # SKIP LOCKED: holds that a capture or another instance is working on are left alone.
        expired = conn.execute(
            "SELECT id, account_id, amount FROM holds WHERE status = 'active' AND expires_at <= now() "
            "ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        ).fetchall()
        if not expired:
            return 0
        totals: dict[int, int] = {}
        for hold in expired:
            totals[hold["account_id"]] = totals.get(hold["account_id"], 0) + hold["amount"]
        self._lock_accounts(conn, set(totals))
        conn.execute(
            "UPDATE holds SET status = 'expired', updated_at = now() WHERE id = ANY(%s)", ([h["id"] for h in expired],)
        )
        conn.execute(
            "UPDATE accounts AS a SET held = a.held - v.total "
            "FROM unnest(%s::bigint[], %s::bigint[]) AS v (id, total) WHERE a.id = v.id",
            (list(totals), list(totals.values())),
        )
        return len(expired)

    def get_hold(self, conn: Connection, hold_id: UUID) -> dict:
        hold = conn.execute(f"SELECT {HOLD_COLUMNS} FROM holds WHERE id = %s", (hold_id,)).fetchone()
        if hold is None:
            raise not_found("HOLD_NOT_FOUND", f"Hold {hold_id} does not exist")
        return hold

    def list_holds(self, conn: Connection, account_id: int, status: str | None = None) -> list[dict]:
        self.get_account(conn, account_id)
        return conn.execute(
            f"SELECT {HOLD_COLUMNS} FROM holds WHERE account_id = %s AND (%s::text IS NULL OR status = %s) "
            f"ORDER BY created_at DESC LIMIT 100",
            (account_id, status, status),
        ).fetchall()

    # --- history ------------------------------------------------------------------

    def get_transaction(self, conn: Connection, transaction_id: UUID) -> dict:
        transaction = conn.execute(
            f"SELECT {TRANSACTION_COLUMNS} FROM transactions WHERE id = %s", (transaction_id,)
        ).fetchone()
        if transaction is None:
            raise not_found("TRANSACTION_NOT_FOUND", f"Transaction {transaction_id} does not exist")
        entries = conn.execute(
            f"SELECT {ENTRY_COLUMNS} FROM entries WHERE transaction_id = %s ORDER BY id", (transaction_id,)
        ).fetchall()
        reversed_by = conn.execute("SELECT id FROM transactions WHERE reverses_id = %s", (transaction_id,)).fetchone()
        return {**transaction, "entries": entries, "reversed_by": reversed_by["id"] if reversed_by else None}

    def recent_transactions(self, conn: Connection, limit: int = 20) -> list[dict]:
        """The latest transactions with their entries: an activity feed, two queries in total."""
        transactions = conn.execute(
            f"""
            SELECT {TRANSACTION_COLUMNS}, (SELECT r.id FROM transactions r WHERE r.reverses_id = t.id) AS reversed_by
              FROM transactions t ORDER BY posted_at DESC, id DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
        entries = conn.execute(
            f"SELECT transaction_id, {ENTRY_COLUMNS} FROM entries WHERE transaction_id = ANY(%s) ORDER BY id",
            ([t["id"] for t in transactions],),
        ).fetchall()
        by_transaction: dict = {}
        for entry in entries:
            by_transaction.setdefault(entry.pop("transaction_id"), []).append(entry)
        return [{**t, "entries": by_transaction.get(t["id"], [])} for t in transactions]

    def statement(self, conn: Connection, account_id: int, limit: int = 50, before_id: int | None = None) -> list[dict]:
        """An account's entries, newest first, each with the running balance after it.

        Keyset pagination (``id < before_id``) stays fast on deep pages, unlike OFFSET,
        which has to walk past every skipped row.
        """
        self.get_account(conn, account_id)
        return conn.execute(
            """
            SELECT e.id, e.transaction_id, e.direction, e.amount, e.currency, e.balance_after, e.posted_at,
                   t.description
              FROM entries e JOIN transactions t ON t.id = e.transaction_id
             WHERE e.account_id = %s AND (%s::bigint IS NULL OR e.id < %s)
             ORDER BY e.id DESC
             LIMIT %s
            """,
            (account_id, before_id, before_id, limit),
        ).fetchall()

    def balance_at(self, conn: Connection, account_id: int, as_of: datetime) -> int:
        """The account's posted balance at a moment in the past, read from the running balance."""
        self.get_account(conn, account_id)
        row = conn.execute(
            "SELECT balance_after FROM entries WHERE account_id = %s AND posted_at <= %s ORDER BY id DESC LIMIT 1",
            (account_id, as_of),
        ).fetchone()
        return row["balance_after"] if row else 0

    # --- locking ------------------------------------------------------------------

    def _lock_accounts(self, conn: Connection, account_ids: set[int]) -> dict[int, dict[str, Any]]:
        # ORDER BY id + FOR UPDATE takes the row locks in ascending id order, the same
        # order in every code path, which is what rules out deadlocks.
        rows = conn.execute(
            f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (sorted(account_ids),),
        ).fetchall()
        found = {row["id"]: row for row in rows}
        missing = sorted(account_ids - found.keys())
        if missing:
            raise not_found("ACCOUNT_NOT_FOUND", f"Account {missing[0]} does not exist")
        return found

    def _lock_active_hold(self, conn: Connection, hold_id: UUID) -> dict:
        hold = conn.execute(
            f"SELECT {HOLD_COLUMNS}, expires_at <= now() AS is_expired FROM holds WHERE id = %s FOR UPDATE",
            (hold_id,),
        ).fetchone()
        if hold is None:
            raise not_found("HOLD_NOT_FOUND", f"Hold {hold_id} does not exist")
        if hold["status"] != "active":
            raise conflict("HOLD_NOT_ACTIVE", f"Hold {hold_id} is already {hold['status']}")
        if hold["is_expired"]:
            raise conflict("HOLD_EXPIRED", f"Hold {hold_id} expired at {hold['expires_at'].isoformat()}")
        return hold
