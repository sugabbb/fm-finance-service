"""Maintains the running operational cashflow balances (cash on hand, waiting approval).

The module keeps a single row per ``operational_cashflow_balances.type``. Every time a
transaction happens on ``operational_cashflows`` (creation, update/correction, submission,
approval/rejection, payment, or payment void) the affected balances are adjusted in place
rather than recomputed from the full history, so the table always has exactly one up to date
row per type.

Rules implemented for ``cash_on_hand`` (the real cash on hand, i.e. only money that actually moved):

- ``MAIN_ACCOUNT_TOPUP`` transactions add ``amount_idr`` to the balance once ``PAID``.
- Any other cashflow type reduces the balance by what was actually paid on it: its full
  ``amount_idr`` once ``PAID``, or only the sum of its non-voided payments while
  ``PARTIALLY_PAID``. Each further payment therefore reduces the balance by its own amount, a
  voided payment gives its amount back, and correcting the amount of a partially paid row does not
  move the balance (no money moved).

Rules implemented for ``waiting_approval``:

- A cashflow adds ``amount_idr`` to the balance while its ``approval_status`` is ``SUBMITTED``
  (a new row saved as submitted, or an existing row moved to submitted).
- Once it leaves ``SUBMITTED`` (``APPROVED`` or ``REJECTED``) the same amount is removed again,
  so the balance always equals the total amount currently waiting for a decision.

Rules implemented for ``approved_unpaid``:

- A cashflow whose ``approval_status`` is ``APPROVED`` contributes what is still unpaid on it:
  its latest ``amount_idr`` minus the sum of its non-voided payments. An unpaid row therefore
  counts in full, a partially paid row counts only its remainder, and a paid row counts nothing.
- Because the remainder is always derived from the current amount and payments, approvals,
  payments, payment voids, edits and Superadmin corrections (including an amount change on an
  approved/partially paid row, or a row sent back to ``DRAFT``) all keep the balance exact.

Every balance follows the same pattern: a pure function gives the contribution of one row in a
given :class:`CashflowState`, a SQL twin computes the same thing for the full-table
recalculation used by the backfill, and :func:`sync_balances` applies the before/after delta on
each write.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import OperationalCashflow, OperationalCashflowBalance, OperationalCashflowPayment

CASH_ON_HAND = "cash_on_hand"
WAITING_APPROVAL = "waiting_approval"
APPROVED_UNPAID = "approved_unpaid"
BALANCE_TYPES = (CASH_ON_HAND, WAITING_APPROVAL, APPROVED_UNPAID)

WAITING_APPROVAL_STATUS = "SUBMITTED"
APPROVED_STATUS = "APPROVED"


@dataclass(frozen=True)
class CashflowState:
    """The parts of an operational cashflow that the balances depend on.

    Capture it with :meth:`of` *before* mutating a row and pass it to :func:`sync_balances`.
    ``paid_amount_idr`` (sum of non-voided payments) is not a column of the cashflow row, which
    is why the audit ``model_snapshot`` is not enough to compute the previous balance effect.
    """

    cashflow_type: str
    approval_status: str
    payment_status: str
    amount_idr: Decimal
    paid_amount_idr: Decimal

    @classmethod
    def of(cls, row: OperationalCashflow) -> CashflowState:
        paid = sum((Decimal(item.amount_idr) for item in row.payments if item.voided_at is None), Decimal("0"))
        return cls(row.cashflow_type, row.approval_status, row.payment_status, Decimal(row.amount_idr), paid)


def get_balance(db: Session, balance_type: str, *, lock: bool = False) -> OperationalCashflowBalance | None:
    stmt = select(OperationalCashflowBalance).where(OperationalCashflowBalance.type == balance_type)
    if lock:
        # populate_existing: a row already in the identity map must be refreshed with the value
        # read under the lock, otherwise the running total could be updated from a stale amount.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.scalar(stmt)


def get_or_create_balance(db: Session, balance_type: str) -> OperationalCashflowBalance:
    """Returns the single balance row for ``balance_type``, creating it with amount 0 if the
    table has no row for that type yet. Locks the row while mutating so concurrent
    transactions cannot corrupt the running total.
    """
    balance = get_balance(db, balance_type, lock=True)
    if balance is None:
        # ON CONFLICT keeps "one row per type" safe even if two sessions seed at the same time.
        db.execute(
            insert(OperationalCashflowBalance)
            .values(type=balance_type, amount=Decimal("0"))
            .on_conflict_do_nothing(index_elements=["type"])
        )
        balance = get_balance(db, balance_type, lock=True)
        assert balance is not None
    return balance


def cashflow_effect(cashflow_type: str, payment_status: str, amount_idr: Decimal, paid_amount_idr: Decimal) -> Decimal:
    """Returns the signed contribution of an operational cashflow transaction, in its current
    persisted state, to the ``cash_on_hand`` balance: a paid top-up adds its amount, any other
    type subtracts what was actually paid (the full amount once ``PAID``, only the non-voided
    payments while ``PARTIALLY_PAID``).
    """
    if cashflow_type == "MAIN_ACCOUNT_TOPUP":
        return amount_idr if payment_status == "PAID" else Decimal("0")
    if payment_status == "PAID":
        return -amount_idr
    if payment_status == "PARTIALLY_PAID":
        return -paid_amount_idr
    return Decimal("0")


def paid_amount_sql() -> ColumnElement[Decimal]:
    """Sum of the non-voided payments of the outer ``operational_cashflows`` row."""
    return (
        select(func.coalesce(func.sum(OperationalCashflowPayment.amount_idr), Decimal("0")))
        .where(
            OperationalCashflowPayment.operational_cashflow_id == OperationalCashflow.id,
            OperationalCashflowPayment.voided_at.is_(None),
        )
        .correlate(OperationalCashflow)
        .scalar_subquery()
    )


def cashflow_effect_sql() -> ColumnElement[Decimal]:
    """SQL twin of :func:`cashflow_effect`, used to recalculate the balance from the whole
    ``operational_cashflows`` table in a single aggregate query. Keep both in sync.
    """
    other_type = OperationalCashflow.cashflow_type != "MAIN_ACCOUNT_TOPUP"
    return case(
        (
            (OperationalCashflow.cashflow_type == "MAIN_ACCOUNT_TOPUP")
            & (OperationalCashflow.payment_status == "PAID"),
            OperationalCashflow.amount_idr,
        ),
        (other_type & (OperationalCashflow.payment_status == "PAID"), -OperationalCashflow.amount_idr),
        (other_type & (OperationalCashflow.payment_status == "PARTIALLY_PAID"), -paid_amount_sql()),
        else_=Decimal("0"),
    )


def waiting_approval_effect(approval_status: str, amount_idr: Decimal) -> Decimal:
    """Returns the contribution of an operational cashflow, in its current persisted state, to
    the ``waiting_approval`` balance: its full amount while submitted, otherwise nothing.
    """
    return amount_idr if approval_status == WAITING_APPROVAL_STATUS else Decimal("0")


def waiting_approval_effect_sql() -> ColumnElement[Decimal]:
    """SQL twin of :func:`waiting_approval_effect`. Keep both in sync."""
    return case(
        (OperationalCashflow.approval_status == WAITING_APPROVAL_STATUS, OperationalCashflow.amount_idr),
        else_=Decimal("0"),
    )


def approved_unpaid_effect(approval_status: str, amount_idr: Decimal, paid_amount_idr: Decimal) -> Decimal:
    """Returns the contribution of an operational cashflow, in its current persisted state, to
    the ``approved_unpaid`` balance: the part of its latest amount that is not paid yet, while
    approved, otherwise nothing.
    """
    return amount_idr - paid_amount_idr if approval_status == APPROVED_STATUS else Decimal("0")


def approved_unpaid_effect_sql() -> ColumnElement[Decimal]:
    """SQL twin of :func:`approved_unpaid_effect`. Keep both in sync."""
    return case(
        (OperationalCashflow.approval_status == APPROVED_STATUS, OperationalCashflow.amount_idr - paid_amount_sql()),
        else_=Decimal("0"),
    )


def calculate_balance(db: Session, effect: ColumnElement[Decimal]) -> tuple[Decimal, int]:
    """Recalculates a balance from scratch over every operational cashflow.

    Returns ``(amount, counted_transactions)`` where ``counted_transactions`` is the number of
    rows that actually contribute to the balance.
    """
    total, counted = db.execute(
        select(
            func.coalesce(func.sum(effect), Decimal("0")),
            func.count().filter(effect != 0),
        ).select_from(OperationalCashflow)
    ).one()
    return Decimal(total), int(counted)


def calculate_cash_on_hand(db: Session) -> tuple[Decimal, int]:
    """Counted transactions are paid top-ups and outflows with money actually paid on them."""
    return calculate_balance(db, cashflow_effect_sql())


def calculate_waiting_approval(db: Session) -> tuple[Decimal, int]:
    """Counted transactions are the cashflows currently ``SUBMITTED``."""
    return calculate_balance(db, waiting_approval_effect_sql())


def calculate_approved_unpaid(db: Session) -> tuple[Decimal, int]:
    """Counted transactions are approved cashflows with an unpaid remainder."""
    return calculate_balance(db, approved_unpaid_effect_sql())


BALANCE_CALCULATORS = {
    CASH_ON_HAND: calculate_cash_on_hand,
    WAITING_APPROVAL: calculate_waiting_approval,
    APPROVED_UNPAID: calculate_approved_unpaid,
}


def cash_on_hand_state_effect(state: CashflowState | None) -> Decimal:
    if state is None:
        return Decimal("0")
    return cashflow_effect(state.cashflow_type, state.payment_status, state.amount_idr, state.paid_amount_idr)


def waiting_approval_state_effect(state: CashflowState | None) -> Decimal:
    if state is None:
        return Decimal("0")
    return waiting_approval_effect(state.approval_status, state.amount_idr)


def approved_unpaid_state_effect(state: CashflowState | None) -> Decimal:
    if state is None:
        return Decimal("0")
    return approved_unpaid_effect(state.approval_status, state.amount_idr, state.paid_amount_idr)


def apply_delta(db: Session, balance_type: str, delta: Decimal) -> None:
    if delta == 0:
        return
    balance = get_or_create_balance(db, balance_type)
    balance.amount = Decimal(balance.amount) + delta
    db.flush()


def sync_cash_on_hand(db: Session, before: CashflowState | None, after: CashflowState) -> None:
    """Applies the delta between the transaction's previous and current state to the
    ``cash_on_hand`` balance. It only ever applies the net change, so it never double counts:
    a partial payment subtracts its own amount, the payment that settles the row subtracts the
    rest, and a void gives the voided amount back.
    """
    apply_delta(db, CASH_ON_HAND, cash_on_hand_state_effect(after) - cash_on_hand_state_effect(before))


def sync_waiting_approval(db: Session, before: CashflowState | None, after: CashflowState) -> None:
    """Moving into ``SUBMITTED`` adds the amount, moving out of it (approve/reject) removes it,
    and correcting the amount of a submitted row applies only the difference.
    """
    apply_delta(db, WAITING_APPROVAL, waiting_approval_state_effect(after) - waiting_approval_state_effect(before))


def sync_approved_unpaid(db: Session, before: CashflowState | None, after: CashflowState) -> None:
    """Approval adds the unpaid remainder, payments reduce it, voids restore it, and amount
    edits/corrections re-derive it from the latest amount minus what is already paid.
    """
    apply_delta(db, APPROVED_UNPAID, approved_unpaid_state_effect(after) - approved_unpaid_state_effect(before))


def sync_balances(db: Session, before: CashflowState | None, row: OperationalCashflow) -> None:
    """Updates every operational cashflow balance after a write to ``row``. ``before`` is the
    :meth:`CashflowState.of` captured before the change, or ``None`` when the row was just
    created. Safe to call after every create/update/correction/submit/decision/payment/void.
    Balances are always locked in the same order to avoid deadlocks between writers.
    """
    after = CashflowState.of(row)
    sync_cash_on_hand(db, before, after)
    sync_waiting_approval(db, before, after)
    sync_approved_unpaid(db, before, after)


def cash_on_hand(db: Session) -> OperationalCashflowBalance:
    """Read-only accessor used by the API. Seeds the row with amount 0 the first time it is
    requested if no transaction has created it yet.
    """
    balance = get_balance(db, CASH_ON_HAND)
    if balance is None:
        balance = get_or_create_balance(db, CASH_ON_HAND)
        db.commit()
    return balance
