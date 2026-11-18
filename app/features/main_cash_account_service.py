"""Maintains the main cash account (``main_cash_accounts``) and its ledger (``main_cash_account_logs``).

Money enters and leaves the main account through these transactions:

- ``operational_cashflows`` with ``cashflow_type = 'MAIN_ACCOUNT_TOPUP'``: the main account tops up
  the operational cash, so once the top-up is ``PAID`` its ``amount_idr`` is a **debit**
  (``current_amount`` decreases).
- ``revenues`` with ``revenue_type = 'MAIN_ACCOUNT_TRANSFER'``: revenue is transferred into the main
  account, so once the transfer is ``PAID`` (approved) its ``amount_idr`` (the net amount: gross minus
  the transfer cost) is a **credit** (``current_amount`` increases).
- ``loan_repayments``: a loan repayment is paid out of the main account, so its ``amount`` is a
  **debit** as long as the repayment is not voided (a void posts the offsetting credit).

Like the operational cashflow balances, the sync is state based: every write compares the source
row's effect before and after the change and records the difference. A Superadmin correction of an
already posted amount therefore produces an offsetting entry (for example a top-up corrected from
1000 to 800 posts a ``credit`` of 200), and the signed sum of the log always equals
``current_amount``.

Posting is asynchronous (transactional outbox): the cashflow/revenue transaction only inserts the
difference into ``main_cash_account_events`` (:func:`enqueue_from_cashflow` /
:func:`enqueue_from_revenue`), without touching or locking the account row, so the API response
does not wait for the main account. ``app.features.main_cash_account_events`` posts the queue in
the background.

Invariant relied on by the worker and the audit: every insert into ``main_cash_account_logs``
happens while holding the account row lock (see :func:`get_or_create_account`), so log ids are
committed in order and a "log id > watermark" query never misses an entry.

``reference`` identifies where an entry comes from:

- ``<source table>:<source id>:<action>`` for automatic entries, for example
  ``operational_cashflows:12:cashflow_created`` or ``revenues:7:transfer_approved``;
- ``manual:<note>`` for adjustments entered through the API (opening balance, bank fees, ...).
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import MainCashAccount, MainCashAccountEvent, MainCashAccountLog, OperationalCashflow, Revenue, User
from app.features.cash_balance_service import CashflowState
from app.features.common import audit

CREDIT = "credit"
DEBIT = "debit"
DIRECTIONS = (CREDIT, DEBIT)

CASHFLOW_SOURCE = "operational_cashflows"
REVENUE_SOURCE = "revenues"
LOAN_REPAYMENT_SOURCE = "loan_repayments"
MANUAL_SOURCE = "manual"
# Sources whose entries reference one row (``<table>:<id>:<action>``).
ROW_SOURCES = (CASHFLOW_SOURCE, REVENUE_SOURCE, LOAN_REPAYMENT_SOURCE)
SOURCES = (*ROW_SOURCES, MANUAL_SOURCE)

TOPUP_TYPE = "MAIN_ACCOUNT_TOPUP"
TRANSFER_TYPE = "MAIN_ACCOUNT_TRANSFER"

SOURCE_REFERENCE = re.compile(rf"^({'|'.join(ROW_SOURCES)}):(\d+):(.*)$", re.DOTALL)


# --- Account row --------------------------------------------------------------------------------


def get_account(db: Session, *, lock: bool = False) -> MainCashAccount | None:
    stmt = select(MainCashAccount)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.scalar(stmt)


def get_or_create_account(db: Session) -> MainCashAccount:
    """Returns the single account row, locked, creating it with amount 0 if it does not exist."""
    account = get_account(db, lock=True)
    if account is None:
        # The singleton unique index turns a concurrent second insert into a no-op.
        db.execute(insert(MainCashAccount).values(current_amount=Decimal("0")).on_conflict_do_nothing())
        account = get_account(db, lock=True)
        assert account is not None
    return account


def main_cash_account(db: Session) -> MainCashAccount:
    """Read accessor used by the API. Seeds the row with amount 0 the first time it is requested."""
    account = get_account(db)
    if account is None:
        account = get_or_create_account(db)
        db.commit()
    return account


# --- Rules --------------------------------------------------------------------------------------


def topup_effect(cashflow_type: str, payment_status: str, amount_idr: Decimal) -> Decimal:
    """Signed effect of an operational cashflow on the main account (top-ups are debits)."""
    return -amount_idr if cashflow_type == TOPUP_TYPE and payment_status == "PAID" else Decimal("0")


def transfer_effect(revenue_type: str, payment_status: str, amount_idr: Decimal) -> Decimal:
    """Signed effect of a revenue on the main account (approved transfers are credits)."""
    return amount_idr if revenue_type == TRANSFER_TYPE and payment_status == "PAID" else Decimal("0")


@dataclass(frozen=True)
class RevenueState:
    """The parts of a revenue the main account depends on. Capture it with :meth:`of` before
    mutating a revenue and pass it to :func:`enqueue_from_revenue`."""

    revenue_type: str
    payment_status: str
    amount_idr: Decimal

    @classmethod
    def of(cls, row: Revenue) -> RevenueState:
        return cls(row.revenue_type, row.payment_status, Decimal(row.amount_idr))


def repayment_effect(voided: bool, amount: Decimal) -> Decimal:
    """Signed effect of a loan repayment on the main account (repayments are debits until voided)."""
    return Decimal("0") if voided else -amount


def cashflow_state_effect(state: CashflowState | None) -> Decimal:
    return Decimal("0") if state is None else topup_effect(state.cashflow_type, state.payment_status, state.amount_idr)


def revenue_state_effect(state: RevenueState | None) -> Decimal:
    return (
        Decimal("0") if state is None else transfer_effect(state.revenue_type, state.payment_status, state.amount_idr)
    )


# --- Posting ------------------------------------------------------------------------------------


def source_reference(source: str, source_id: int, action: str) -> str:
    return f"{source}:{source_id}:{action}"


def manual_reference(note: str) -> str:
    return f"{MANUAL_SOURCE}:{note}"


def parse_reference(reference: str) -> dict[str, object]:
    """Splits a reference into ``{"type", "id", "action"}`` for API consumers."""
    match = SOURCE_REFERENCE.match(reference)
    if match:
        return {"type": match[1], "id": int(match[2]), "action": match[3]}
    if reference.startswith(f"{MANUAL_SOURCE}:"):
        return {"type": MANUAL_SOURCE, "id": None, "action": None}
    return {"type": None, "id": None, "action": None}


def post_entry(db: Session, delta: Decimal, reference: str) -> MainCashAccountLog | None:
    """Moves ``current_amount`` by ``delta`` and records it in the log (positive = credit,
    negative = debit). Does nothing for a zero delta. Locks the account row before inserting.
    """
    if delta == 0:
        return None
    account = get_or_create_account(db)
    entry = MainCashAccountLog(
        main_cash_account_id=account.id,
        amount=abs(delta),
        direction=CREDIT if delta > 0 else DEBIT,
        reference=reference,
    )
    db.add(entry)
    account.current_amount = Decimal(account.current_amount) + delta
    db.flush()
    return entry


def enqueue(db: Session, source: str, source_id: int, action: str, delta: Decimal) -> MainCashAccountEvent | None:
    """Queues a main account movement in the caller's transaction. Cheap and lock free: it does not
    touch the account row, so cashflow/revenue writes never wait on the main account."""
    if delta == 0:
        return None
    event = MainCashAccountEvent(source_table=source, source_id=source_id, action=action, delta=delta)
    db.add(event)
    return event


def enqueue_from_cashflow(
    db: Session, before: CashflowState | None, row: OperationalCashflow, action: str
) -> MainCashAccountEvent | None:
    """Queues the change of an operational cashflow's effect on the main account, if any."""
    delta = cashflow_state_effect(CashflowState.of(row)) - cashflow_state_effect(before)
    return enqueue(db, CASHFLOW_SOURCE, row.id, action, delta)


def enqueue_from_revenue(
    db: Session, before: RevenueState | None, row: Revenue, action: str
) -> MainCashAccountEvent | None:
    """Queues the change of a revenue's effect on the main account, if any."""
    delta = revenue_state_effect(RevenueState.of(row)) - revenue_state_effect(before)
    return enqueue(db, REVENUE_SOURCE, row.id, action, delta)


def adjust(db: Session, direction: str, amount: Decimal, note: str, actor: User) -> MainCashAccountLog:
    """Manual credit/debit entered through the API (for example the opening balance or a bank
    fee that does not come from operational cashflows or revenues). Posted immediately; it only
    needs the account row lock, not the cashflow/revenue write lock. It reaches ``cash_statements``
    the next time the nightly sync runs (see app.scripts.sync_cash_statements)."""
    delta = amount if direction == CREDIT else -amount
    entry = post_entry(db, delta, manual_reference(note))
    assert entry is not None
    audit(db, entry, "main_cash_account_adjusted", actor, reason=note)
    return entry


# --- Reconciliation (used by the backfill command) ----------------------------------------------


def signed_log_amount() -> ColumnElement[Decimal]:
    return case((MainCashAccountLog.direction == CREDIT, MainCashAccountLog.amount), else_=-MainCashAccountLog.amount)


def log_total(db: Session) -> Decimal:
    return Decimal(db.scalar(select(func.coalesce(func.sum(signed_log_amount()), Decimal("0")))) or 0)
