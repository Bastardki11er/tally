"""The ledger: accounts, balanced transactions, holds and reversals.

Every method takes an open connection and runs inside the caller's database
transaction, so the API layer can combine an operation with its idempotency
record atomically.

Who writes what: this module inserts transactions, entries and holds, and changes
hold statuses. The database derives everything else (migration 005): inserting an
entry moves the account's balance and fills in balance_after, and creating or
settling a hold moves the account's held amount. The checks here exist to fail
early with a precise error; the database enforces the same rules regardless.

Concurrency: every operation locks the account rows it touches with
``SELECT ... FOR UPDATE``, always in ascending id order. Two transfers between
the same accounts in opposite directions therefore queue up instead of
deadlocking, and a balance is checked and changed under the same lock, so no
update is ever lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.types.json import Jsonb

from .errors import conflict, not_found, unprocessable
from .observability import LOCK_WAIT_SECONDS

Direction = Literal["debit", "credit"]
AccountType = Literal["asset", "liability", "equity", "revenue", "expense"]

ACCOUNT_COLUMNS = (
    "id, name, type, normal_side, currency, allow_negative, balance, held, available, metadata, created_at"
)
TRANSACTION_COLUMNS = "id, description, metadata, reverses_id, posted_at"
ENTRY_COLUMNS = "id, account_id, direction, amount, currency, balance_after, posted_at"
EXPIRY_BATCH_SIZE = 500

HOLD_COLUMNS = (
    "id, account_id, amount, status, description, expires_at, captured_transaction_id, created_at, updated_at"
)


def require_positive_amount(amount: object, what: str = "amount") -> None:
    # bool is a subclass of int in Python: True would otherwise pass as 1.
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise unprocessable("INVALID_AMOUNT", f"{what} must be a positive whole number of minor units, got {amount!r}")


@dataclass(frozen=True)
class Leg:
    """One side of a transaction: move ``amount`` minor units on ``account_id``."""

    account_id: int
    direction: Direction
    amount: int

    def __post_init__(self) -> None:
        # Fail here with a clear error rather than later on a database CHECK.
        if self.direction not in ("debit", "credit"):
            raise unprocessable("INVALID_ENTRY", f"direction must be 'debit' or 'credit', got {self.direction!r}")
        require_positive_amount(self.amount)


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
        """Posts a balanced transaction; the database applies each entry to its account's balance."""
        accounts = self._lock_accounts(conn, {leg.account_id for leg in legs})
        self._check_postable(accounts, legs)
        transaction = self._insert_transaction(conn, description, metadata, reverses_id)
        return self._insert_entries(conn, transaction, accounts, legs)

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
        require_positive_amount(amount)
        # A naive datetime would be read in the database session's time zone, which can
        # shift the expiry by hours without any error.
        if expires_at.tzinfo is None:
            raise unprocessable("INVALID_EXPIRY", "expires_at must include a time zone")
        if expires_at <= datetime.now(UTC):
            raise unprocessable("INVALID_EXPIRY", f"expires_at must be in the future, got {expires_at.isoformat()}")
        account = self._lock_accounts(conn, {account_id})[account_id]
        if not account["allow_negative"] and account["available"] < amount:
            raise conflict(
                "INSUFFICIENT_FUNDS", f"Account {account_id} has {account['available']} available, hold needs {amount}"
            )
        # The database adds an active hold to the account's held amount.
        return conn.execute(
            f"INSERT INTO holds (account_id, amount, expires_at, description) VALUES (%s, %s, %s, %s) "
            f"RETURNING {HOLD_COLUMNS}",
            (account_id, amount, expires_at, description),
        ).fetchone()

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
        require_positive_amount(amount, "capture amount")
        if amount > hold["amount"]:
            raise unprocessable("CAPTURE_EXCEEDS_HOLD", f"Cannot capture {amount}, the hold is for {hold['amount']}")
        if destination_account_id == hold["account_id"]:
            raise unprocessable("SAME_ACCOUNT", "Destination must differ from the held account")

        accounts = self._lock_accounts(conn, {hold["account_id"], destination_account_id})
        source = accounts[hold["account_id"]]
        # Spending moves the held account away from its normal side, whatever its type.
        out = opposite(source["normal_side"])
        legs = [Leg(hold["account_id"], out, amount), Leg(destination_account_id, opposite(out), amount)]
        # Check against the funds as they will be once this hold's reservation is
        # released; counting the reserved amount as unavailable would count it twice.
        released = {**accounts, source["id"]: {**source, "held": source["held"] - hold["amount"]}}
        self._check_postable(released, legs)

        transaction = self._insert_transaction(conn, description or hold["description"], {"hold_id": str(hold_id)})
        # Marking the hold captured releases its reservation (database trigger), so it
        # happens before the entries move the balance.
        captured = conn.execute(
            f"UPDATE holds SET status = 'captured', captured_transaction_id = %s "
            f"WHERE id = %s RETURNING {HOLD_COLUMNS}",
            (transaction["id"], hold_id),
        ).fetchone()
        transaction = self._insert_entries(conn, transaction, accounts, legs)
        return {"hold": captured, "transaction": transaction}

    def void_hold(self, conn: Connection, hold_id: UUID) -> dict:
        self._lock_active_hold(conn, hold_id)
        # The database gives the amount back to the account's available funds.
        return conn.execute(
            f"UPDATE holds SET status = 'voided' WHERE id = %s RETURNING {HOLD_COLUMNS}", (hold_id,)
        ).fetchone()

    def expire_holds(self, conn: Connection, limit: int = EXPIRY_BATCH_SIZE) -> int:
        """Releases holds whose expiry has passed. Safe to run on several instances at once."""
        # SKIP LOCKED: holds that a capture or another instance is working on are left alone.
        expired = conn.execute(
            "SELECT id, account_id FROM holds WHERE status = 'active' AND expires_at <= now() "
            "ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        ).fetchall()
        if not expired:
            return 0
        # The trigger releasing each hold updates its account. Take those row locks first,
        # in id order like every other path, rather than in whatever order holds expire.
        self._lock_accounts(conn, {hold["account_id"] for hold in expired})
        conn.execute("UPDATE holds SET status = 'expired' WHERE id = ANY(%s)", ([h["id"] for h in expired],))
        return len(expired)

    def get_hold(self, conn: Connection, hold_id: UUID) -> dict:
        hold = conn.execute(f"SELECT {HOLD_COLUMNS} FROM holds WHERE id = %s", (hold_id,)).fetchone()
        if hold is None:
            raise not_found("HOLD_NOT_FOUND", f"Hold {hold_id} does not exist")
        return hold

    def list_holds(
        self,
        conn: Connection,
        account_id: int,
        status: str | None = None,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[dict]:
        """An account's holds, newest first. ``before`` is the (created_at, id) of the last hold seen."""
        self.get_account(conn, account_id)
        after_cursor = "AND (created_at, id) < (%s, %s)" if before else ""
        return conn.execute(
            f"SELECT {HOLD_COLUMNS} FROM holds WHERE account_id = %s AND (%s::text IS NULL OR status = %s) "
            f"{after_cursor} ORDER BY created_at DESC, id DESC LIMIT %s",
            (account_id, status, status, *(before or ()), limit),
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

    def recent_transactions(
        self, conn: Connection, limit: int = 20, before: tuple[datetime, UUID] | None = None
    ) -> list[dict]:
        """The latest transactions with their entries, two queries per page.

        ``before`` is the (posted_at, id) of the last transaction seen. The row comparison
        walks the (posted_at, id) index backwards from there, however deep the page.
        """
        after_cursor = "WHERE (t.posted_at, t.id) < (%s, %s)" if before else ""
        transactions = conn.execute(
            f"""
            SELECT {TRANSACTION_COLUMNS}, (SELECT r.id FROM transactions r WHERE r.reverses_id = t.id) AS reversed_by
              FROM transactions t {after_cursor}
             ORDER BY t.posted_at DESC, t.id DESC LIMIT %s
            """,
            (*(before or ()), limit),
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
        # Within one account posted_at rises with id (migration 003 guarantees it), so
        # this is "the latest entry at or before as_of", answered by one probe of the
        # (account_id, posted_at, id) index however far back as_of is.
        row = conn.execute(
            "SELECT balance_after FROM entries WHERE account_id = %s AND posted_at <= %s "
            "ORDER BY posted_at DESC, id DESC LIMIT 1",
            (account_id, as_of),
        ).fetchone()
        return row["balance_after"] if row else 0

    # --- internals ----------------------------------------------------------------

    def _check_postable(self, accounts: dict[int, dict[str, Any]], legs: list[Leg]) -> None:
        """Validates legs against the locked accounts before anything is written."""
        if len(legs) < 2:
            raise unprocessable("TOO_FEW_ENTRIES", "A transaction needs at least two entries")
        currencies = [accounts[leg.account_id]["currency"] for leg in legs]
        if len(legs) == 2 and currencies[0] != currencies[1]:
            raise unprocessable(
                "CURRENCY_MISMATCH", f"Cannot move money between {currencies[0]} and {currencies[1]} accounts"
            )

        net: dict[str, int] = {}
        for leg, currency in zip(legs, currencies, strict=True):
            net[currency] = net.get(currency, 0) + (leg.amount if leg.direction == "debit" else -leg.amount)
        unbalanced = {currency: amount for currency, amount in net.items() if amount != 0}
        if unbalanced:
            detail = ", ".join(f"{c} debits - credits = {n}" for c, n in unbalanced.items())
            raise unprocessable("UNBALANCED", f"Debits must equal credits in every currency ({detail})")

        # Walk the legs the way the database will apply them, one entry at a time: a
        # protected account may not dip below zero at any step, not just at the end.
        balances = {account_id: account["balance"] for account_id, account in accounts.items()}
        for leg in legs:
            account = accounts[leg.account_id]
            balances[leg.account_id] += leg.amount if leg.direction == account["normal_side"] else -leg.amount
            if not account["allow_negative"] and balances[leg.account_id] - account["held"] < 0:
                raise conflict(
                    "INSUFFICIENT_FUNDS",
                    f"Account {leg.account_id} has {account['balance'] - account['held']} available, "
                    f"this transaction needs {account['balance'] - balances[leg.account_id]}",
                )

    def _insert_transaction(
        self, conn: Connection, description: str, metadata: dict | None, reverses_id: UUID | None = None
    ) -> dict:
        return conn.execute(
            f"INSERT INTO transactions (description, metadata, reverses_id) VALUES (%s, %s, %s) "
            f"RETURNING {TRANSACTION_COLUMNS}",
            (description, Jsonb(metadata or {}), reverses_id),
        ).fetchone()

    def _insert_entries(
        self, conn: Connection, transaction: dict, accounts: dict[int, dict[str, Any]], legs: list[Leg]
    ) -> dict:
        # WITH ORDINALITY ... ORDER BY n: entries go in, and hit the balance trigger, in
        # exactly the order of the legs.
        entries = conn.execute(
            f"""
            INSERT INTO entries (transaction_id, account_id, direction, amount, currency)
            SELECT %s, account_id, direction, amount, currency
              FROM unnest(%s::bigint[], %s::text[], %s::bigint[], %s::char(3)[])
                   WITH ORDINALITY AS leg (account_id, direction, amount, currency, n)
             ORDER BY n
            RETURNING {ENTRY_COLUMNS}
            """,
            (
                transaction["id"],
                [leg.account_id for leg in legs],
                [leg.direction for leg in legs],
                [leg.amount for leg in legs],
                [accounts[leg.account_id]["currency"] for leg in legs],
            ),
        ).fetchall()
        return {**transaction, "entries": sorted(entries, key=lambda e: e["id"])}

    def _lock_accounts(self, conn: Connection, account_ids: set[int]) -> dict[int, dict[str, Any]]:
        # ORDER BY id + FOR UPDATE takes the row locks in ascending id order, the same
        # order in every code path, which is what rules out deadlocks.
        with LOCK_WAIT_SECONDS.time():
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
