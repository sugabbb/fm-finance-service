"""Populates ``cash_statements``: every real cash movement of the business, like a bank statement.

A statement row is written only when money actually moves, dated on the day it moved, with
``credit`` = money in and ``debit`` = money out. Three sources feed it:

``operational_cashflows`` (every type except ``MAIN_ACCOUNT_TOPUP``) -> **debit**
    Once the cashflow is ``PARTIALLY_PAID`` or ``PAID``: one row per non-voided payment, for the
    amount of that payment, on its ``paid_date`` (``source_line`` = ``payment:<payment id>``). A
    cashflow paid in three parts therefore has three rows, numbered in their description
    ("partial payment 1", "partial payment 2", "final payment 3") and sharing the same
    ``reference``. A ``PAID`` cashflow without any payment row (data recorded outside the API) is
    one row for its full amount on its paid date (``paid``).

``revenues`` (every type except ``MAIN_ACCOUNT_TRANSFER``) -> **credit**
    Same rules: one row per non-voided payment of a ``PARTIALLY_PAID``/``PAID`` revenue, or one
    row for the full amount of a ``PAID`` revenue without payment rows - in practice an approved
    ``DIRECT_TRANSFER`` sale, dated on its transfer date (``paid``).

``revenues`` of type ``MAIN_ACCOUNT_TRANSFER`` -> **debit of the fee only**
    The transfer itself only moves money between the business's own accounts, so it is not a
    statement row; its ``payment_cost_idr`` is money that leaves the business, recorded once the
    transfer is ``PAID`` (approved) on its paid date (``fee``).

``main_cash_account_logs`` -> the manual adjustments only
    Entries referenced ``manual:<note>``, in their own direction, on the date they were posted
    (``manual``). The entries posted automatically for top-ups, transfers and loan repayments are
    not included (loan repayments have their own rows, below).

``loans`` (their non-voided ``loan_repayments``) -> **debit**
    One row per repayment, for its full amount (principal + interest), on its ``paid_date``
    (``LOAN_REPAYMENT``, ``repayment:<id>``), like the money that left the bank. The
    principal/interest split stays in the loan tables. All rows of a loan share the reference
    ``loans:<loan id>``; ``payment_id`` is the repayment and ``payment_sequence`` its number. The
    source details are the loan's start date, principal and ``PARTIALLY_PAID``/``PAID`` (paid off).

The top-ups (``MAIN_ACCOUNT_TOPUP``) are excluded for the same reason as the transfers: they move
money from the main account to the operational cash, both of which belong to the business.

Every row also records where it comes from: ``reference`` (``<source table>:<source id>``, the
same for all rows of a transaction), ``source_table``/``source_id``/``source_line``,
``payment_id``/``payment_sequence`` for payment rows, and the current ``source_transaction_date``,
``source_amount`` (for a transfer fee, the transfer's gross amount) and
``source_payment_status`` of the transaction.

Each rule is a SQL ``SELECT`` producing the statement rows of its source, so the table is written
set-based by :func:`apply_sync`, either for every transaction or for a :class:`SyncScope` (a list
of transactions per source; all of their rows). It is idempotent: a row whose values did not change
is left untouched (its ``id`` is stable), rows whose source no longer qualifies (voided payment,
corrected amount, ...) are updated or deleted.

Unlike the cash on hand/approved-unpaid balances and the main cash account log, this table is
**not** kept current inside the request that changes a source: nothing reads it from a write path,
so the API does not wait for it. Two entry points populate it:

- :func:`sync_period`, the nightly job (``app.scripts.sync_cash_statements``, cron at midnight in
  ``APP_TIMEZONE``): one month only - the transactions :func:`period_scope` finds for it, never the
  whole history. Each run is recorded in ``cash_statement_sync_runs``
  (``app.features.cash_statement_sync``);
- :func:`rebuild_cash_statements`, the one-off backfill (``app.scripts.backfill_cash_statements``):
  every transaction ever recorded, after ``alembic upgrade head`` or to repair drift.

Both take the same advisory lock as the writes, so they are safe while the API is live, and both
are idempotent. The finance/monthly-report module that reads this table tolerates the statement
being up to a day behind the source transactions.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    CompoundSelect,
    Date,
    Integer,
    Numeric,
    Select,
    SQLColumnExpression,
    String,
    Text,
    and_,
    bindparam,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by, insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    CashStatement,
    Customer,
    Lender,
    Loan,
    LoanRepayment,
    MainCashAccountLog,
    OperationalCashflow,
    OperationalCashflowBankAdminDetail,
    OperationalCashflowFeedDetail,
    OperationalCashflowHealthDetail,
    OperationalCashflowLivestockPurchaseDetail,
    OperationalCashflowOtherDetail,
    OperationalCashflowPayment,
    OperationalCashflowSalaryDetail,
    OperationalCashflowUtilityDetail,
    Revenue,
    RevenueFishDetail,
    RevenueOtherDetail,
    RevenuePayment,
    RevenueTransferDetail,
)
from app.features.locks import lock_balances
from app.features.main_cash_account_service import CREDIT, DEBIT, MANUAL_SOURCE, TOPUP_TYPE, TRANSFER_TYPE

CASHFLOW_SOURCE = "operational_cashflows"
REVENUE_SOURCE = "revenues"
LOG_SOURCE = "main_cash_account_logs"
LOAN_SOURCE = "loans"
SOURCES = (CASHFLOW_SOURCE, REVENUE_SOURCE, LOG_SOURCE, LOAN_SOURCE)

PAID_STATUSES = ("PARTIALLY_PAID", "PAID")
FEE_CATEGORY = "TRANSFER_FEE"
MANUAL_CATEGORY = "MANUAL_ADJUSTMENT"
LOAN_REPAYMENT_CATEGORY = "LOAN_REPAYMENT"

# Columns written by the sync, in SELECT order.
COLUMNS = (
    "statement_date",
    "direction",
    "amount",
    "category",
    "description",
    "counterparty",
    "reference",
    "source_table",
    "source_id",
    "source_line",
    "payment_id",
    "payment_sequence",
    "source_transaction_date",
    "source_amount",
    "source_payment_status",
)
KEY_COLUMNS = ("source_table", "source_id", "source_line")
# Everything except the key: a change in any of them makes a stored row stale.
VALUE_COLUMNS = tuple(name for name in COLUMNS if name not in KEY_COLUMNS)
COLUMN_TYPES: dict[str, Any] = {
    "statement_date": Date(),
    "direction": String(10),
    "amount": Numeric(18, 0),
    "category": String(40),
    "description": Text(),
    "counterparty": String(255),
    "reference": String(80),
    "source_table": String(40),
    "source_id": BigInteger(),
    "source_line": String(60),
    "payment_id": BigInteger(),
    "payment_sequence": Integer(),
    "source_transaction_date": Date(),
    "source_amount": Numeric(18, 0),
    "source_payment_status": String(20),
}

Expression = SQLColumnExpression[Any]


# --- Building blocks ----------------------------------------------------------------------------


def local_date(timestamp: Expression) -> ColumnElement[date]:
    """Business date of a timestamp, in the application timezone."""
    return cast(func.timezone(settings.app_timezone, timestamp), Date)


def paid_date(paid_at: Expression, transaction_date: Expression) -> ColumnElement[date]:
    """Date a transaction without payment rows was paid (its transaction date if never stamped)."""
    return func.coalesce(local_date(paid_at), transaction_date)


def with_notes(base: str | Expression, notes: Expression) -> ColumnElement[str]:
    """``base`` followed by ``: notes`` when the notes are not empty."""
    prefix = literal(base) if isinstance(base, str) else base
    return prefix + func.coalesce(literal(": ") + func.nullif(notes, ""), "")


def reference(source_table: str, source_id: Expression) -> ColumnElement[str]:
    return literal(f"{source_table}:") + cast(source_id, String)


def statement_row(**values: Any) -> list[ColumnElement[Any]]:
    """The SELECT list of one rule: every column of ``COLUMNS`` (missing ones are NULL), cast to
    the table's type and labelled, so all rules can be combined with UNION ALL."""
    unknown = set(values) - set(COLUMNS)
    if unknown:
        raise ValueError(f"unknown cash statement columns {sorted(unknown)}")
    columns: list[ColumnElement[Any]] = []
    for name in COLUMNS:
        value = values.get(name)
        expression = literal(value) if value is None or isinstance(value, str | int) else value
        columns.append(cast(expression, COLUMN_TYPES[name]).label(name))
    return columns


def payment_suffix(
    sequence: Expression, count: Expression, cumulative: Expression, total: Expression
) -> ColumnElement[str]:
    """Tells the payments of one transaction apart in their description: nothing when a single
    payment settled it, otherwise "partial payment <n>" and, for the one that settled it,
    "final payment <n>"."""
    number = cast(sequence, String) + literal(")")
    return case(
        (and_(count == 1, cumulative >= total), literal("")),
        (cumulative >= total, literal(" (final payment ") + number),
        else_=literal(" (partial payment ") + number,
    )


@dataclass(frozen=True)
class PaymentWindow:
    """Sequence, count and running total of the non-voided payments of each transaction."""

    sequence: ColumnElement[Any]
    count: ColumnElement[Any]
    cumulative: ColumnElement[Any]

    @classmethod
    def over(
        cls, transaction_id: Expression, paid_on: Expression, payment_id: Expression, amount: Expression
    ) -> PaymentWindow:
        order = (paid_on, payment_id)
        return cls(
            func.row_number().over(partition_by=transaction_id, order_by=order),
            func.count().over(partition_by=transaction_id),
            func.sum(amount).over(partition_by=transaction_id, order_by=order),
        )


# --- Rules: operational cashflows ---------------------------------------------------------------


def cashflow_base() -> tuple[Select[Any], Expression, Expression]:
    """``SELECT FROM operational_cashflows`` joined to its detail tables, with the description and
    counterparty of a cashflow. Shared by the cashflow rules."""
    cashflow = OperationalCashflow
    salary, utility, health, other = (
        OperationalCashflowSalaryDetail,
        OperationalCashflowUtilityDetail,
        OperationalCashflowHealthDetail,
        OperationalCashflowOtherDetail,
    )
    livestock, bank = OperationalCashflowLivestockPurchaseDetail, OperationalCashflowBankAdminDetail
    feed_names = (
        select(
            func.string_agg(
                OperationalCashflowFeedDetail.feed_type_name,
                aggregate_order_by(literal(", "), OperationalCashflowFeedDetail.feed_type_name),
            )
        )
        .select_from(OperationalCashflowFeedDetail)
        .where(OperationalCashflowFeedDetail.operational_cashflow_id == cashflow.id)
        .correlate(cashflow)
        .scalar_subquery()
    )
    description = case(
        (cashflow.cashflow_type == "FEED_AYAM", with_notes("Feed purchase Ayam", feed_names)),
        (cashflow.cashflow_type == "FEED_LELE", with_notes("Feed purchase Lele", feed_names)),
        (cashflow.cashflow_type == "SALARY", with_notes("Salary", salary.salary_period)),
        (cashflow.cashflow_type == "UTILITY", with_notes("Utility", utility.description)),
        (
            cashflow.cashflow_type == "HEALTH",
            with_notes(literal("Health ") + func.initcap(health.species), health.description),
        ),
        (cashflow.cashflow_type == "OTHER", with_notes("Other expense", other.description)),
        (cashflow.cashflow_type == "LELE_SEED_PURCHASE", with_notes("Lele seed purchase", livestock.notes)),
        (cashflow.cashflow_type == "PULLET_PURCHASE", with_notes("Pullet purchase", livestock.notes)),
        (cashflow.cashflow_type == "BANK_ADMIN_FEE", with_notes("Bank admin fee", bank.notes)),
        else_=cashflow.cashflow_type,
    )
    stmt = (
        select(cashflow)
        .outerjoin(salary, salary.operational_cashflow_id == cashflow.id)
        .outerjoin(utility, utility.operational_cashflow_id == cashflow.id)
        .outerjoin(health, health.operational_cashflow_id == cashflow.id)
        .outerjoin(other, other.operational_cashflow_id == cashflow.id)
        .outerjoin(livestock, livestock.operational_cashflow_id == cashflow.id)
        .outerjoin(bank, bank.operational_cashflow_id == cashflow.id)
        .where(cashflow.cashflow_type != TOPUP_TYPE)
    )
    return stmt, description, salary.employee_name


def cashflow_rules(ids: Sequence[int] | None) -> list[Select[Any]]:
    cashflow, payment = OperationalCashflow, OperationalCashflowPayment
    base, description, counterparty = cashflow_base()
    if ids is not None:
        base = base.where(id_in(cashflow.id, ids))
    source = {
        "category": cashflow.cashflow_type,
        "counterparty": counterparty,
        "reference": reference(CASHFLOW_SOURCE, cashflow.id),
        "source_table": CASHFLOW_SOURCE,
        "source_id": cashflow.id,
        "source_transaction_date": cashflow.transaction_date,
        "source_amount": cashflow.amount_idr,
        "source_payment_status": cashflow.payment_status,
    }
    window = PaymentWindow.over(payment.operational_cashflow_id, payment.paid_date, payment.id, payment.amount_idr)
    payments = (
        base.join(payment, payment.operational_cashflow_id == cashflow.id)
        .with_only_columns(
            *statement_row(
                **source,
                statement_date=payment.paid_date,
                direction=DEBIT,
                amount=payment.amount_idr,
                description=description
                + payment_suffix(window.sequence, window.count, window.cumulative, cashflow.amount_idr),
                source_line=literal("payment:") + cast(payment.id, String),
                payment_id=payment.id,
                payment_sequence=window.sequence,
            )
        )
        .where(cashflow.payment_status.in_(PAID_STATUSES), payment.voided_at.is_(None), payment.amount_idr > 0)
    )
    has_payment = exists().where(payment.operational_cashflow_id == cashflow.id, payment.voided_at.is_(None))
    settled_without_payments = base.with_only_columns(
        *statement_row(
            **source,
            statement_date=paid_date(cashflow.paid_at, cashflow.transaction_date),
            direction=DEBIT,
            amount=cashflow.amount_idr,
            description=description,
            source_line="paid",
        )
    ).where(cashflow.payment_status == "PAID", cashflow.amount_idr > 0, ~has_payment)
    return [payments, settled_without_payments]


# --- Rules: revenues ----------------------------------------------------------------------------


def revenue_rules(ids: Sequence[int] | None) -> list[Select[Any]]:
    revenue, payment, transfer = Revenue, RevenuePayment, RevenueTransferDetail
    other, fish = RevenueOtherDetail, RevenueFishDetail
    description = case(
        (revenue.revenue_type == "EGG_SALE", literal("Egg sale")),
        (revenue.revenue_type == "FISH_SALE", with_notes("Fish sale", fish.notes)),
        (revenue.revenue_type == "OTHER", with_notes("Other revenue", other.notes)),
        else_=revenue.revenue_type,
    )
    base = (
        select(revenue)
        .outerjoin(Customer, Customer.id == revenue.customer_id)
        .outerjoin(other, other.revenue_id == revenue.id)
        .outerjoin(fish, fish.revenue_id == revenue.id)
        .outerjoin(transfer, transfer.revenue_id == revenue.id)
    )
    if ids is not None:
        base = base.where(id_in(revenue.id, ids))
    source = {
        "counterparty": Customer.name,
        "reference": reference(REVENUE_SOURCE, revenue.id),
        "source_table": REVENUE_SOURCE,
        "source_id": revenue.id,
        "source_transaction_date": revenue.transaction_date,
        "source_amount": revenue.amount_idr,
        "source_payment_status": revenue.payment_status,
    }
    sale = revenue.revenue_type != TRANSFER_TYPE
    window = PaymentWindow.over(payment.revenue_id, payment.paid_date, payment.id, payment.amount_idr)
    payments = (
        base.join(payment, payment.revenue_id == revenue.id)
        .with_only_columns(
            *statement_row(
                **source,
                statement_date=payment.paid_date,
                direction=CREDIT,
                amount=payment.amount_idr,
                category=revenue.revenue_type,
                description=description
                + payment_suffix(window.sequence, window.count, window.cumulative, revenue.amount_idr),
                source_line=literal("payment:") + cast(payment.id, String),
                payment_id=payment.id,
                payment_sequence=window.sequence,
            )
        )
        .where(sale, revenue.payment_status.in_(PAID_STATUSES), payment.voided_at.is_(None), payment.amount_idr > 0)
    )
    has_payment = exists().where(payment.revenue_id == revenue.id, payment.voided_at.is_(None))
    settled_without_payments = base.with_only_columns(
        *statement_row(
            **source,
            statement_date=paid_date(revenue.paid_at, revenue.transaction_date),
            direction=CREDIT,
            amount=revenue.amount_idr,
            category=revenue.revenue_type,
            description=description
            + case((revenue.payment_method == "DIRECT_TRANSFER", literal(" (direct transfer)")), else_=literal("")),
            source_line="paid",
        )
    ).where(sale, revenue.payment_status == "PAID", revenue.amount_idr > 0, ~has_payment)
    transfer_fee = base.with_only_columns(
        *statement_row(
            **(source | {"counterparty": None, "source_amount": transfer.gross_amount_idr}),
            statement_date=paid_date(revenue.paid_at, revenue.transaction_date),
            direction=DEBIT,
            amount=transfer.payment_cost_idr,
            category=FEE_CATEGORY,
            description=with_notes("Main account transfer fee", transfer.notes),
            source_line="fee",
        )
    ).where(revenue.revenue_type == TRANSFER_TYPE, revenue.payment_status == "PAID", transfer.payment_cost_idr > 0)
    return [payments, settled_without_payments, transfer_fee]


# --- Rules: main cash account adjustments -------------------------------------------------------


def manual_rule(ids: Sequence[int] | None) -> Select[Any]:
    log = MainCashAccountLog
    prefix = f"{MANUAL_SOURCE}:"
    posted_on = local_date(log.created_at)
    stmt = select(
        *statement_row(
            statement_date=posted_on,
            direction=log.direction,
            amount=log.amount,
            category=MANUAL_CATEGORY,
            description=with_notes("Main account adjustment", func.substr(log.reference, len(prefix) + 1)),
            reference=reference(LOG_SOURCE, log.id),
            source_table=LOG_SOURCE,
            source_id=log.id,
            source_line="manual",
            source_transaction_date=posted_on,
            source_amount=log.amount,
        )
    ).where(func.starts_with(log.reference, prefix))
    return stmt.where(id_in(log.id, ids)) if ids is not None else stmt


# --- Rules: loan repayments ---------------------------------------------------------------------


def loan_rules(ids: Sequence[int] | None) -> list[Select[Any]]:
    loan, repayment = Loan, LoanRepayment
    lender = Lender.name + func.coalesce(literal(" ") + func.nullif(loan.reference_number, ""), "")
    first, last = cast(repayment.first_installment, String), cast(repayment.last_installment, String)
    # A short-term loan has a single installment: nothing to tell apart.
    installments = func.coalesce(
        case(
            (loan.loan_type == "SHORT_TERM", literal("")),
            (repayment.first_installment == repayment.last_installment, literal(" (installment ") + first + ")"),
            else_=literal(" (installments ") + first + "-" + last + ")",
        ),
        "",
    )
    sequence = func.row_number().over(partition_by=repayment.loan_id, order_by=(repayment.paid_date, repayment.id))
    stmt = (
        select(repayment)
        .join(loan, loan.id == repayment.loan_id)
        .join(Lender, Lender.id == loan.lender_id)
        .with_only_columns(
            *statement_row(
                statement_date=repayment.paid_date,
                direction=DEBIT,
                amount=repayment.amount,
                category=LOAN_REPAYMENT_CATEGORY,
                description=literal("Loan repayment: ") + lender + installments,
                counterparty=Lender.name,
                reference=reference(LOAN_SOURCE, loan.id),
                source_table=LOAN_SOURCE,
                source_id=loan.id,
                source_line=literal("repayment:") + cast(repayment.id, String),
                payment_id=repayment.id,
                payment_sequence=sequence,
                source_transaction_date=loan.start_date,
                source_amount=loan.principal_amount,
                source_payment_status=case(
                    (loan.status == "PAID_OFF", literal("PAID")), else_=literal("PARTIALLY_PAID")
                ),
            )
        )
        .where(repayment.voided_at.is_(None))
    )
    # Scoped by loan, so the repayment sequence is still numbered over all of a loan's repayments.
    return [stmt.where(id_in(repayment.loan_id, ids)) if ids is not None else stmt]


@dataclass(frozen=True)
class SyncScope:
    """The source transactions a sync is limited to: ids per source table. A table missing from
    ``ids`` (or with no ids) is not touched at all."""

    ids: Mapping[str, Sequence[int]]

    def __post_init__(self) -> None:
        unknown = set(self.ids) - set(SOURCES)
        if unknown:
            raise ValueError(f"unknown cash statement sources {sorted(unknown)}")

    def of(self, source_table: str) -> Sequence[int]:
        return self.ids.get(source_table, ())

    @property
    def total(self) -> int:
        return sum(len(ids) for ids in self.ids.values())


def id_in(column: Expression, ids: Sequence[int]) -> ColumnElement[bool]:
    """``column = ANY(:ids)``: one array parameter instead of thousands of expanded IN parameters."""
    return column == func.any(bindparam("ids", value=list(ids), type_=ARRAY(BigInteger), unique=True))


def expected_statements(scope: SyncScope | None = None) -> CompoundSelect[Any]:
    """Every statement row the sources currently justify; with a ``scope``, only the rows of the
    transactions in it (all of their rows, whatever their date). The scope must not be empty."""
    rules: list[Select[Any]] = []
    builders: tuple[tuple[str, Callable[[Sequence[int] | None], list[Select[Any]]]], ...] = (
        (CASHFLOW_SOURCE, cashflow_rules),
        (REVENUE_SOURCE, revenue_rules),
        (LOG_SOURCE, lambda ids: [manual_rule(ids)]),
        (LOAN_SOURCE, loan_rules),
    )
    for source_table, build in builders:
        if scope is None:
            rules.extend(build(None))
        elif scope.of(source_table):
            rules.extend(build(scope.of(source_table)))
    return union_all(*rules)


# --- Sync ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncPlan:
    """What a sync would change: rows to insert, stored rows whose values changed, and stored rows
    whose source no longer justifies them."""

    inserted: int
    updated: int
    deleted: int

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.updated or self.deleted)


def source_filter(scope: SyncScope | None) -> list[ColumnElement[bool]]:
    """The stored rows a sync may touch: all of them, or those of the transactions in ``scope``."""
    if scope is None:
        return []
    return [
        or_(
            *(
                and_(CashStatement.source_table == source_table, id_in(CashStatement.source_id, ids))
                for source_table, ids in scope.ids.items()
                if ids
            )
        )
    ]


def plan_sync(db: Session, scope: SyncScope | None = None) -> SyncPlan:
    """Compares the expected rows with the stored ones inside PostgreSQL (FULL OUTER JOIN) and
    returns only the counts, so nothing but the numbers is loaded into Python."""
    if scope is not None and not scope.total:
        return SyncPlan(0, 0, 0)
    expected = expected_statements(scope).subquery("expected")
    existing = select(CashStatement).where(*source_filter(scope)).subquery("existing")
    key_match = and_(*(expected.c[name] == existing.c[name] for name in KEY_COLUMNS))
    differs = or_(*(expected.c[name].is_distinct_from(existing.c[name]) for name in VALUE_COLUMNS))
    inserted, updated, deleted = db.execute(
        select(
            func.count().filter(existing.c.id.is_(None)),
            func.count().filter(existing.c.id.is_not(None), expected.c.source_table.is_not(None), differs),
            func.count().filter(expected.c.source_table.is_(None)),
        ).select_from(expected.outerjoin(existing, key_match, full=True))
    ).one()
    return SyncPlan(int(inserted), int(updated), int(deleted))


def apply_sync(db: Session, scope: SyncScope | None = None) -> None:
    """Upserts the expected rows (touching only those whose values changed) and deletes the stored
    rows that are no longer expected. Set-based: two statements whatever the row count."""
    if scope is not None and not scope.total:
        return
    expected = expected_statements(scope)
    upsert = insert(CashStatement).from_select(list(COLUMNS), expected)
    db.execute(
        upsert.on_conflict_do_update(
            index_elements=list(KEY_COLUMNS),
            set_={name: upsert.excluded[name] for name in VALUE_COLUMNS} | {"updated_at": func.now()},
            where=or_(
                *(getattr(CashStatement, name).is_distinct_from(upsert.excluded[name]) for name in VALUE_COLUMNS)
            ),
        )
    )
    still_expected = expected.subquery("expected")
    db.execute(
        delete(CashStatement)
        .where(*source_filter(scope))
        .where(~exists().where(*(still_expected.c[name] == getattr(CashStatement, name) for name in KEY_COLUMNS)))
    )


@dataclass(frozen=True)
class SourceTotal:
    source_table: str
    rows: int
    credit: Decimal
    debit: Decimal

    @property
    def net(self) -> Decimal:
        return self.credit - self.debit


def source_totals(db: Session) -> list[SourceTotal]:
    """Row count, total credit and total debit per source table, in ``SOURCES`` order."""
    rows = db.execute(
        select(
            CashStatement.source_table,
            func.count(),
            func.coalesce(func.sum(CashStatement.amount).filter(CashStatement.direction == CREDIT), Decimal("0")),
            func.coalesce(func.sum(CashStatement.amount).filter(CashStatement.direction == DEBIT), Decimal("0")),
        ).group_by(CashStatement.source_table)
    ).all()
    found = {
        table: SourceTotal(table, int(count), Decimal(credit), Decimal(debit)) for table, count, credit, debit in rows
    }
    return [found.get(table, SourceTotal(table, 0, Decimal("0"), Decimal("0"))) for table in SOURCES]


@dataclass(frozen=True)
class RebuildResult:
    plan: SyncPlan
    totals: list[SourceTotal]
    dry_run: bool


def rebuild_cash_statements(db: Session, *, dry_run: bool = False) -> RebuildResult:
    """Populates/repairs the whole table from every source (the one-off
    ``app.scripts.backfill_cash_statements``; the nightly job uses :func:`sync_period` instead).

    Takes the same transactional lock as cashflow/revenue writes so it sees a consistent picture
    while the API is live. Commits unless ``dry_run`` (then rolls back after reporting what would
    change and the totals the table would have).
    """
    try:
        lock_balances(db)
        plan = plan_sync(db)
        apply_sync(db)
        totals = source_totals(db)
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return RebuildResult(plan, totals, dry_run)
    except Exception:
        db.rollback()
        raise


# --- Monthly sync (the nightly job) -------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    """A calendar month, in ``APP_TIMEZONE``: the unit the nightly sync works on."""

    start: date

    def __post_init__(self) -> None:
        if self.start.day != 1:
            raise ValueError("a period starts on the first day of a month")

    @classmethod
    def month_of(cls, day: date) -> Period:
        return cls(day.replace(day=1))

    @classmethod
    def parse(cls, value: str) -> Period:
        """``YYYY-MM``."""
        try:
            year, month = (int(part) for part in value.split("-"))
            return cls(date(year, month, 1))
        except ValueError as exc:
            raise ValueError(f"expected a month as YYYY-MM, got {value!r}") from exc

    @property
    def next_start(self) -> date:
        return date(self.start.year + self.start.month // 12, self.start.month % 12 + 1, 1)

    @property
    def end(self) -> date:
        return self.next_start - timedelta(days=1)

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m}"

    @property
    def starts_at(self) -> datetime:
        """Local midnight of the first day."""
        return datetime.combine(self.start, time.min, tzinfo=settings.timezone)

    @property
    def ends_at(self) -> datetime:
        """Local midnight of the first day of the next month (exclusive)."""
        return datetime.combine(self.next_start, time.min, tzinfo=settings.timezone)

    @property
    def changed_since(self) -> datetime:
        """Source changes from this moment on are in scope: the month plus one day before it, so a
        write committed just around midnight of the last run of the previous month is never lost
        between two runs."""
        return self.starts_at - CHANGE_OVERLAP


# See Period.changed_since.
CHANGE_OVERLAP = timedelta(days=1)


# How far back from the job's start time the month is read (see nightly_period).
NIGHTLY_LOOKBACK = timedelta(minutes=30)


def nightly_period(started_at: datetime) -> Period:
    """The month a run started at ``started_at`` syncs: the month of ``started_at - 30 minutes``,
    in ``APP_TIMEZONE``.

    The job is scheduled at 00:00, when the day (and on the 1st, the month) has only just started
    and has no data yet. Reading the month half an hour earlier makes the run at 00:00 on 1 August
    sync July (23:30 on 31 July) - the month whose last day just ended - instead of an August that
    is still empty; every other night it is simply the current month. A run that starts a little
    late (up to 30 minutes) still lands on the same month."""
    if started_at.tzinfo is None:
        raise ValueError("started_at must be timezone-aware")
    return Period.month_of((started_at.astimezone(settings.timezone) - NIGHTLY_LOOKBACK).date())


def period_scope(db: Session, period: Period) -> SyncScope:
    """The transactions of ``period``, per source table. A transaction is in scope when:

    - money moved on it during the month (a payment, repayment, paid date or adjustment dated in
      it) - the rows the month's statement should have;
    - it, or one of its payments/repayments (or its lender), was changed during the month
      (``updated_at`` since :attr:`Period.changed_since`) - e.g. a payment of last month voided
      this month, or a correction - so the month's edits reach the statement whatever their date;
    - it already has a statement row dated in the month - so a row whose source disappeared or
      moved away is removed.

    Only indexed lookups of ids: the rules themselves then run for these transactions only, and
    for all of their rows (so e.g. "partial payment 2" is still numbered across months).
    """
    first, last, since = period.start, period.end, period.changed_since
    until = period.ends_at

    def on_statement(source_table: str) -> Select[Any]:
        return select(CashStatement.source_id).where(
            CashStatement.source_table == source_table, CashStatement.statement_date.between(first, last)
        )

    def paid_in_month(paid_at: Any, transaction_date: Any) -> ColumnElement[bool]:
        # Same date as paid_date(): the local date of paid_at, else the transaction date.
        return or_(
            and_(paid_at >= period.starts_at, paid_at < until),
            and_(paid_at.is_(None), transaction_date.between(first, last)),
        )

    def ids(*parts: Select[Any]) -> list[int]:
        union = parts[0].union(*parts[1:]).subquery()
        return [int(value) for value in db.scalars(select(union.c[0]).order_by(union.c[0])).all()]

    cashflow, cashflow_payment = OperationalCashflow, OperationalCashflowPayment
    revenue, revenue_payment = Revenue, RevenuePayment
    log, loan, repayment = MainCashAccountLog, Loan, LoanRepayment
    payment_touched = or_(cashflow_payment.paid_date.between(first, last), cashflow_payment.updated_at >= since)
    revenue_payment_touched = or_(revenue_payment.paid_date.between(first, last), revenue_payment.updated_at >= since)
    return SyncScope(
        {
            CASHFLOW_SOURCE: ids(
                select(cashflow_payment.operational_cashflow_id)
                .join(cashflow, cashflow.id == cashflow_payment.operational_cashflow_id)
                .where(payment_touched, cashflow.cashflow_type != TOPUP_TYPE),
                select(cashflow.id).where(
                    cashflow.cashflow_type != TOPUP_TYPE,
                    or_(cashflow.updated_at >= since, paid_in_month(cashflow.paid_at, cashflow.transaction_date)),
                ),
                on_statement(CASHFLOW_SOURCE),
            ),
            REVENUE_SOURCE: ids(
                select(revenue_payment.revenue_id).where(revenue_payment_touched),
                select(revenue.id).where(
                    or_(revenue.updated_at >= since, paid_in_month(revenue.paid_at, revenue.transaction_date))
                ),
                on_statement(REVENUE_SOURCE),
            ),
            # Log entries are immutable: posted in the month (or its overlap) is the only change.
            LOG_SOURCE: ids(
                select(log.id).where(func.starts_with(log.reference, f"{MANUAL_SOURCE}:"), log.created_at >= since),
                on_statement(LOG_SOURCE),
            ),
            LOAN_SOURCE: ids(
                select(repayment.loan_id).where(
                    or_(repayment.paid_date.between(first, last), repayment.updated_at >= since)
                ),
                select(loan.id).where(loan.updated_at >= since),
                # A renamed lender changes the description/counterparty of all of its loans' rows.
                select(loan.id).join(Lender, Lender.id == loan.lender_id).where(Lender.updated_at >= since),
                on_statement(LOAN_SOURCE),
            ),
        }
    )


@dataclass(frozen=True)
class PeriodTotals:
    rows: int
    credit: Decimal
    debit: Decimal


def period_totals(db: Session, period: Period) -> PeriodTotals:
    """Row count and money in/out of the month's statement."""
    rows, credit, debit = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(CashStatement.amount).filter(CashStatement.direction == CREDIT), Decimal("0")),
            func.coalesce(func.sum(CashStatement.amount).filter(CashStatement.direction == DEBIT), Decimal("0")),
        ).where(CashStatement.statement_date.between(period.start, period.end))
    ).one()
    return PeriodTotals(int(rows), Decimal(credit), Decimal(debit))


@dataclass(frozen=True)
class PeriodSyncResult:
    period: Period
    scope: SyncScope
    plan: SyncPlan
    totals: PeriodTotals
    dry_run: bool


def sync_period(db: Session, period: Period, *, dry_run: bool = False) -> PeriodSyncResult:
    """Brings the statement rows of ``period``'s transactions (see :func:`period_scope`) up to date:
    the engine of the nightly ``app.scripts.sync_cash_statements``.

    Same lock, idempotency and dry-run behaviour as :func:`rebuild_cash_statements`, but it derives
    only the month's transactions instead of every transaction ever recorded. Rows of those
    transactions dated in other months are brought up to date too (their numbering and details
    depend on the whole transaction), everything else is left alone.
    """
    try:
        lock_balances(db)
        scope = period_scope(db, period)
        plan = plan_sync(db, scope)
        apply_sync(db, scope)
        totals = period_totals(db, period)
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return PeriodSyncResult(period, scope, plan, totals, dry_run)
    except Exception:
        db.rollback()
        raise
