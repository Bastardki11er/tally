"""Model-based testing with Hypothesis.

Hypothesis generates random sequences of ledger operations (open accounts,
deposits, transfers, holds, captures, voids, reversals) and runs them against
the real database. Alongside, a tiny in-memory model predicts what each
operation should do. After every single step we check that:

  * reconciliation is clean: every transaction balances and every stored
    balance equals the sum of its entries
  * the database agrees with the model, balance for balance
  * no protected account is overdrawn

When a sequence breaks a rule, Hypothesis shrinks it to the shortest failing
sequence and prints it.
"""

from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, precondition, rule, run_state_machine_as_test

from tally.errors import LedgerError
from tally.ledger import Leg, opposite
from tally.reconciliation import reconcile

from .conftest import Books, reset

amounts = st.integers(min_value=1, max_value=5_000)


def ledger_machine(db):
    books = Books(db)

    class LedgerMachine(RuleBasedStateMachine):
        accounts = Bundle("accounts")

        def __init__(self):
            super().__init__()
            reset(db)
            # The outside world: money enters and leaves the system through it.
            self.world = books.create_account("World", "equity", "USD", allow_negative=True)["id"]
            self.model = {self.world: {"balance": 0, "held": 0, "normal": "credit", "allow_negative": True}}
            self.holds: dict = {}  # hold id -> (account id, amount)
            self.reversible: list = []  # (transaction id, legs)

        # --- model helpers ---------------------------------------------------------

        def effect(self, legs):
            """New balances if the legs were posted, or None if a protected account would be overdrawn."""
            balances = {a: m["balance"] for a, m in self.model.items()}
            for leg in legs:
                balances[leg.account_id] += (
                    leg.amount if leg.direction == self.model[leg.account_id]["normal"] else -leg.amount
                )
            for account_id, balance in balances.items():
                m = self.model[account_id]
                if not m["allow_negative"] and balance - m["held"] < 0:
                    return None
            return balances

        def apply(self, balances):
            for account_id, balance in balances.items():
                self.model[account_id]["balance"] = balance

        def post(self, legs, attempt):
            expected = self.effect(legs)
            try:
                result = attempt()
            except LedgerError as error:
                assert error.code == "INSUFFICIENT_FUNDS", error.code
                assert expected is None, "the ledger refused a transaction the model allows"
                return None
            assert expected is not None, "the ledger allowed an overdraft the model forbids"
            self.apply(expected)
            return result

        # --- rules -----------------------------------------------------------------

        @rule(
            target=accounts, kind=st.sampled_from(["asset", "liability", "revenue", "expense"]), negative=st.booleans()
        )
        def open_account(self, kind, negative):
            account = books.create_account(f"{kind} account", kind, "USD", allow_negative=negative)
            self.model[account["id"]] = {
                "balance": 0,
                "held": 0,
                "allow_negative": negative,
                "normal": "debit" if kind in ("asset", "expense") else "credit",
            }
            return account["id"]

        @rule(account=accounts, amount=amounts)
        def deposit(self, account, amount):
            grow = self.model[account]["normal"]
            legs = [Leg(account, grow, amount), Leg(self.world, opposite(grow), amount)]
            transaction = self.post(legs, lambda: books.post(legs))
            self.reversible.append((transaction["id"], legs))

        @rule(debit=accounts, credit=accounts, amount=amounts)
        def transfer(self, debit, credit, amount):
            if debit == credit:
                return
            legs = [Leg(debit, "debit", amount), Leg(credit, "credit", amount)]
            transaction = self.post(legs, lambda: books.transfer(debit, credit, amount))
            if transaction:
                self.reversible.append((transaction["id"], legs))

        @rule(account=accounts, amount=amounts)
        def hold(self, account, amount):
            m = self.model[account]
            allowed = m["allow_negative"] or m["balance"] - m["held"] >= amount
            try:
                hold = books.create_hold(account, amount, datetime.now(UTC) + timedelta(days=1))
            except LedgerError as error:
                assert error.code == "INSUFFICIENT_FUNDS" and not allowed
                return
            assert allowed
            m["held"] += amount
            self.holds[hold["id"]] = (account, amount)

        @precondition(lambda self: self.holds)
        @rule(data=st.data(), destination=accounts)
        def capture(self, data, destination):
            hold_id = data.draw(st.sampled_from(list(self.holds)))  # creation order: deterministic on replay
            account, held = self.holds[hold_id]
            if destination == account:
                return
            amount = data.draw(st.integers(min_value=1, max_value=held))
            self.model[account]["held"] -= held  # released before the spend is checked
            out = opposite(self.model[account]["normal"])
            legs = [Leg(account, out, amount), Leg(destination, opposite(out), amount)]
            result = self.post(legs, lambda: books.capture_hold(hold_id, destination, amount))
            if result is None:  # the whole capture rolled back, hold included
                self.model[account]["held"] += held
                return
            del self.holds[hold_id]
            self.reversible.append((result["transaction"]["id"], legs))

        @precondition(lambda self: self.holds)
        @rule(data=st.data())
        def void(self, data):
            hold_id = data.draw(st.sampled_from(list(self.holds)))  # creation order: deterministic on replay
            account, held = self.holds.pop(hold_id)
            books.void_hold(hold_id)
            self.model[account]["held"] -= held

        @precondition(lambda self: self.reversible)
        @rule(data=st.data())
        def reverse(self, data):
            index = data.draw(st.integers(min_value=0, max_value=len(self.reversible) - 1))
            transaction_id, legs = self.reversible[index]
            mirror = [Leg(leg.account_id, opposite(leg.direction), leg.amount) for leg in legs]
            if self.post(mirror, lambda: books.reverse(transaction_id)) is not None:
                self.reversible.pop(index)

        # --- invariants ------------------------------------------------------------

        @invariant()
        def books_reconcile(self):
            report = db.run(reconcile)
            assert report["ok"], report

        @invariant()
        def database_matches_model(self):
            rows = db.run(
                lambda conn: conn.execute(
                    "SELECT id, balance, held, available, allow_negative FROM accounts"
                ).fetchall()
            )
            for row in rows:
                m = self.model[row["id"]]
                assert (row["balance"], row["held"]) == (m["balance"], m["held"]), (row, m)
                assert row["allow_negative"] or row["available"] >= 0

    return LedgerMachine


def test_random_operation_sequences_keep_the_books_correct(db):
    run_state_machine_as_test(
        ledger_machine(db),
        settings=settings(
            max_examples=40,
            stateful_step_count=30,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
        ),
    )
