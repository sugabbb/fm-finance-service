"""Monthly cut-off: the month-end close for the finance team, aggregated from ``cash_statements``.

Run at midnight at the end of every month (``app.scripts.monthly_cutoff``), for the month that
just ended. It first syncs that month's cash statement (so the close never works on a statement
that is up to a day behind), then computes, **as of the last day of the month**:

Cash (actual cash on hand)
    ``cash_opening`` + ``opening_balances_in_period`` + ``cash_in`` - ``cash_out`` = ``cash_closing``.
    ``cash_in``/``cash_out`` are the month's statement credits/debits; ``cash_opening`` is the net of
    every statement row before the month plus the go-live opening balances
    (``financial_opening_balances``) effective before it.

P&L (cash basis), from the month's statement rows by source
    revenue = credits of ``revenues`` (egg, fish and other sales, per category); operating
    expenses = debits of ``operational_cashflows`` (per category); other expenses = the main
    account transfer fees; finance costs = the interest part of the month's loan repayments.

Loans
    Every loan not cancelled and started by the end of the month, with its principal/interest paid
    and outstanding and its overdue installments, computed from the repayments dated up to the end
    of the month (the same allocation as the loan module: oldest installment first, interest
    before principal). Repayments made later do not change a closed month.

Receivables
    Per customer, what the sales dated up to the end of the month still had to be paid at that
    date, aged by sale date (0-30, 31-60, 61-90, over 90 days). Direct-transfer sales not approved
    yet are listed apart (``pending_verification``), rejected ones are ignored.

Categories
    The month's statement rows grouped by direction, source table and category.

Potential revenue (``app.features.month_end_assets``)
    The egg stock and fish weight estimate at the month's end, from operational-service's private
    API, valued at the month's average sales price per kg. The cut-off **waits for them**: while
    operational answers ``404`` for the month (not finalized yet), nothing is computed or recorded
    (:class:`MonthEndDataNotReadyError`) and the month stays open; the next run tries again.

**No double counting.** The business has one pool of money in several pockets (main cash account,
operational cash on hand, sales cash not deposited yet). Moves between pockets (top-ups, transfers)
are not on the statement, so the statement's credits/debits *are* the business's cash movements:

- ``cash_closing`` never adds ``main_cash_accounts.current_amount`` or the ``cash_on_hand`` balance,
  which are parts of the same money. The only figures added that are not statement rows are the
  go-live opening balances, which no statement row covers; the main account's own opening balance
  is a manual adjustment and therefore already a statement row;
- a ``LOAN_REPAYMENT`` statement row is principal + interest. The P&L takes only the interest (from
  the loan allocation) and never the row itself; the principal is financing, listed as ``NON_PL``.
  A warning is recorded if the month's repayments and the statement's ``LOAN_REPAYMENT`` rows differ;
- collections of receivables are the statement's credit rows of each sale, not ``revenue_payments``
  counted again;
- manual main account adjustments (opening balance, loan proceeds, ...) are cash but cannot be
  classified as income or expense, so they are listed as ``NON_PL`` lines, not in the P&L;
- potential revenue is neither cash nor income: it is reported on its own, never added to
  ``cash_closing``, revenue or net profit.

The P&L therefore reconciles with the cash movement:
``net_profit - loan principal + manual in - manual out = cash_in - cash_out`` (checked every run).

Every run is a new immutable snapshot in ``monthly_cutoffs`` and its detail tables; the latest
successful one per month has ``is_latest``. Re-running a closed month (``--month``) after late
entries restates it without losing the earlier figures. Everything is computed from the data as it
is when the run happens (e.g. a payment voided later is gone), as of the month's last day.

The reported snapshot of a month is read through ``GET /api/v1/monthly-cutoffs?year=&month=``
(``app.features.monthly_cutoffs``), which never computes anything itself.
"""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, case, cast, func, literal, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    CashStatement,
    Customer,
    FinancialOpeningBalance,
    Loan,
    MonthlyCutoff,
    MonthlyCutoffAsset,
    MonthlyCutoffCategory,
    MonthlyCutoffLoan,
    MonthlyCutoffProfitLoss,
    MonthlyCutoffReceivable,
    Revenue,
)
from app.db.session import SessionLocal
from app.features.cash_statement_service import (
    CASHFLOW_SOURCE,
    LOAN_REPAYMENT_CATEGORY,
    LOAN_SOURCE,
    LOG_SOURCE,
    REVENUE_SOURCE,
    Period,
    nightly_period,
)
from app.features.cash_statement_sync import run_sync
from app.features.common import now_utc
from app.features.loan_service import CANCELLED, active_repayments, allocate, loan_query
from app.features.locks import lock_balances
from app.features.main_cash_account_service import CREDIT, DEBIT, TRANSFER_TYPE
from app.features.month_end_assets import (
    EGG_STOCK,
    FISH_STOCK,
    MonthEndAssets,
    PotentialRevenue,
    fetch_month_end_assets,
    value_assets,
)

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
RUNNING, SUCCESS, FAILED = "RUNNING", "SUCCESS", "FAILED"

REVENUE_SECTION = "REVENUE"
OPERATING_EXPENSE = "OPERATING_EXPENSE"
OTHER_EXPENSE = "OTHER_EXPENSE"
FINANCE_COST = "FINANCE_COST"
NON_PL = "NON_PL"
SECTIONS = (REVENUE_SECTION, OPERATING_EXPENSE, OTHER_EXPENSE, FINANCE_COST, NON_PL)
LOAN_INTEREST_LINE = "LOAN_INTEREST"
LOAN_PRINCIPAL_LINE = "LOAN_PRINCIPAL"
MANUAL_IN_LINE = "MANUAL_ADJUSTMENT_IN"
MANUAL_OUT_LINE = "MANUAL_ADJUSTMENT_OUT"

NO_CUSTOMER = "(no customer)"
# Sales that count as receivables, and those paid by direct transfer still waiting for approval.
RECEIVABLE_APPROVALS = ("NOT_REQUIRED", "APPROVED")
PENDING_APPROVAL = "SUBMITTED"


class MonthNotClosedError(ValueError):
    """The month asked for has not ended yet."""


# --- Figures ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CashPosition:
    opening: Decimal
    opening_balances_in_period: Decimal
    cash_in: Decimal
    cash_out: Decimal
    opening_balances_total: Decimal
    rows: int

    @property
    def closing(self) -> Decimal:
        return self.opening + self.opening_balances_in_period + self.cash_in - self.cash_out


@dataclass(frozen=True)
class CategoryTotal:
    direction: str
    source_table: str
    category: str
    rows: int
    amount: Decimal


@dataclass(frozen=True)
class ProfitLossLine:
    section: str
    line: str
    amount: Decimal


@dataclass(frozen=True)
class ProfitLoss:
    lines: list[ProfitLossLine]

    def total(self, section: str) -> Decimal:
        return sum((item.amount for item in self.lines if item.section == section), ZERO)

    def line(self, section: str, line: str) -> Decimal:
        return sum((item.amount for item in self.lines if (item.section, item.line) == (section, line)), ZERO)

    @property
    def net_profit(self) -> Decimal:
        return (
            self.total(REVENUE_SECTION)
            - self.total(OPERATING_EXPENSE)
            - self.total(OTHER_EXPENSE)
            - self.total(FINANCE_COST)
        )


@dataclass(frozen=True)
class LoanPosition:
    loan_id: int
    lender_id: int
    lender_name: str
    reference_number: str | None
    loan_type: str
    start_date: Any
    maturity_date: Any
    principal_amount: Decimal
    principal_paid: Decimal
    interest_total: Decimal
    interest_paid: Decimal
    overdue_installments: int
    overdue_amount: Decimal
    principal_repaid_in_period: Decimal
    interest_repaid_in_period: Decimal

    @property
    def principal_outstanding(self) -> Decimal:
        return self.principal_amount - self.principal_paid

    @property
    def interest_outstanding(self) -> Decimal:
        return self.interest_total - self.interest_paid


@dataclass(frozen=True)
class CustomerReceivable:
    customer_id: int | None
    customer_name: str
    invoices: int
    outstanding: Decimal
    aging_0_30: Decimal
    aging_31_60: Decimal
    aging_61_90: Decimal
    aging_over_90: Decimal
    pending_verification: Decimal


@dataclass
class CutoffFigures:
    period: Period
    cash: CashPosition
    categories: list[CategoryTotal]
    profit_loss: ProfitLoss
    loans: list[LoanPosition]
    receivables: list[CustomerReceivable]
    potential_revenue: PotentialRevenue
    warnings: list[str] = field(default_factory=list)

    @property
    def loans_outstanding(self) -> list[LoanPosition]:
        return [item for item in self.loans if item.principal_outstanding or item.interest_outstanding]


# --- Queries ------------------------------------------------------------------------------------


def signed_amount() -> Any:
    return case((CashStatement.direction == CREDIT, CashStatement.amount), else_=-CashStatement.amount)


def cash_position(db: Session, period: Period) -> CashPosition:
    """The month's statement totals and the running balance before it, plus the go-live opening
    balances (the only cash that is not on the statement)."""
    in_period = CashStatement.statement_date.between(period.start, period.end)
    before, cash_in, cash_out, rows = db.execute(
        select(
            func.coalesce(func.sum(signed_amount()).filter(CashStatement.statement_date < period.start), ZERO),
            func.coalesce(func.sum(CashStatement.amount).filter(in_period, CashStatement.direction == CREDIT), ZERO),
            func.coalesce(func.sum(CashStatement.amount).filter(in_period, CashStatement.direction == DEBIT), ZERO),
            func.count().filter(in_period),
        )
    ).one()
    opening = FinancialOpeningBalance
    openings_before, openings_in_period = db.execute(
        select(
            func.coalesce(func.sum(opening.amount_idr).filter(opening.effective_date < period.start), ZERO),
            func.coalesce(
                func.sum(opening.amount_idr).filter(opening.effective_date.between(period.start, period.end)), ZERO
            ),
        )
    ).one()
    return CashPosition(
        opening=Decimal(before) + Decimal(openings_before),
        opening_balances_in_period=Decimal(openings_in_period),
        cash_in=Decimal(cash_in),
        cash_out=Decimal(cash_out),
        opening_balances_total=Decimal(openings_before) + Decimal(openings_in_period),
        rows=int(rows),
    )


def category_totals(db: Session, period: Period) -> list[CategoryTotal]:
    statement = CashStatement
    rows = db.execute(
        select(
            statement.direction, statement.source_table, statement.category, func.count(), func.sum(statement.amount)
        )
        .where(statement.statement_date.between(period.start, period.end))
        .group_by(statement.direction, statement.source_table, statement.category)
        .order_by(statement.direction, statement.source_table, statement.category)
    ).all()
    return [
        CategoryTotal(direction, source_table, category, int(count), Decimal(amount))
        for direction, source_table, category, count, amount in rows
    ]


def loan_positions(db: Session, period: Period) -> list[LoanPosition]:
    """Loans as of the month's last day: the repayments dated up to then, allocated like the loan
    module does (in payment order, so a repayment's split never depends on later ones). Loans with
    nothing left to pay and no repayment during the month are left out."""
    loans = db.scalars(
        loan_query().where(Loan.start_date <= period.end, Loan.status != CANCELLED).order_by(Loan.start_date, Loan.id)
    ).all()
    positions: list[LoanPosition] = []
    for loan in loans:
        installments = sorted(loan.installments, key=lambda item: item.sequence)
        repayments = [item for item in active_repayments(loan) if item.paid_date <= period.end]
        paid, splits = allocate(
            [(Decimal(item.principal_amount), Decimal(item.interest_amount)) for item in installments],
            [Decimal(item.amount) for item in repayments],
        )
        in_period = [
            split for repayment, split in zip(repayments, splits, strict=True) if repayment.paid_date >= period.start
        ]
        overdue = [
            Decimal(item.principal_amount + item.interest_amount) - state.principal_paid - state.interest_paid
            for item, state in zip(installments, paid, strict=True)
            if item.due_date <= period.end
        ]
        overdue = [amount for amount in overdue if amount > 0]
        position = LoanPosition(
            loan_id=loan.id,
            lender_id=loan.lender_id,
            lender_name=loan.lender.name,
            reference_number=loan.reference_number,
            loan_type=loan.loan_type,
            start_date=loan.start_date,
            maturity_date=installments[-1].due_date,
            principal_amount=Decimal(loan.principal_amount),
            principal_paid=sum((state.principal_paid for state in paid), ZERO),
            interest_total=sum((Decimal(item.interest_amount) for item in installments), ZERO),
            interest_paid=sum((state.interest_paid for state in paid), ZERO),
            overdue_installments=len(overdue),
            overdue_amount=sum(overdue, ZERO),
            principal_repaid_in_period=sum((split.principal for split in in_period), ZERO),
            interest_repaid_in_period=sum((split.interest for split in in_period), ZERO),
        )
        owed = position.principal_outstanding + position.interest_outstanding
        if owed or in_period:
            positions.append(position)
    return positions


def customer_receivables(db: Session, period: Period) -> list[CustomerReceivable]:
    """What every customer still owed on the month's last day, computed in PostgreSQL: each sale
    dated up to then, minus what the statement shows collected on it up to then."""
    end = period.end
    collected = (
        select(CashStatement.source_id.label("revenue_id"), func.sum(CashStatement.amount).label("amount"))
        .where(
            CashStatement.source_table == REVENUE_SOURCE,
            CashStatement.direction == CREDIT,
            CashStatement.statement_date <= end,
        )
        .group_by(CashStatement.source_id)
        .subquery("collected")
    )
    sales = (
        select(
            Revenue.customer_id.label("customer_id"),
            Revenue.approval_status.label("approval_status"),
            cast(literal(end) - Revenue.transaction_date, Integer).label("age"),
            (Revenue.amount_idr - func.coalesce(collected.c.amount, ZERO)).label("due"),
        )
        .outerjoin(collected, collected.c.revenue_id == Revenue.id)
        .where(
            Revenue.revenue_type != TRANSFER_TYPE,
            Revenue.transaction_date <= end,
            Revenue.approval_status.in_((*RECEIVABLE_APPROVALS, PENDING_APPROVAL)),
        )
        .subquery("sales")
    )
    receivable = sales.c.approval_status != PENDING_APPROVAL

    def aged(low: int, high: int | None) -> Any:
        condition = [receivable, sales.c.age >= low] + ([sales.c.age <= high] if high is not None else [])
        return func.coalesce(func.sum(sales.c.due).filter(*condition), ZERO)

    rows = db.execute(
        select(
            sales.c.customer_id,
            func.coalesce(func.min(Customer.name), NO_CUSTOMER),
            func.count(),
            func.coalesce(func.sum(sales.c.due).filter(receivable), ZERO),
            aged(0, 30),
            aged(31, 60),
            aged(61, 90),
            aged(91, None),
            func.coalesce(func.sum(sales.c.due).filter(~receivable), ZERO),
        )
        .select_from(sales.outerjoin(Customer, Customer.id == sales.c.customer_id))
        .where(sales.c.due > 0)
        .group_by(sales.c.customer_id)
        .order_by(func.sum(sales.c.due).desc(), sales.c.customer_id)
    ).all()
    return [
        CustomerReceivable(
            customer_id=customer_id,
            customer_name=name,
            invoices=int(invoices),
            outstanding=Decimal(outstanding),
            aging_0_30=Decimal(a030),
            aging_31_60=Decimal(a3160),
            aging_61_90=Decimal(a6190),
            aging_over_90=Decimal(a90),
            pending_verification=Decimal(pending),
        )
        for customer_id, name, invoices, outstanding, a030, a3160, a6190, a90, pending in rows
    ]


def profit_and_loss(
    categories: Sequence[CategoryTotal], loans: Sequence[LoanPosition], warnings: list[str]
) -> ProfitLoss:
    """Classifies the month's statement categories by source (see the module docstring)."""
    lines: list[ProfitLossLine] = []
    for item in categories:
        kind = (item.direction, item.source_table)
        if kind == (CREDIT, REVENUE_SOURCE):
            lines.append(ProfitLossLine(REVENUE_SECTION, item.category, item.amount))
        elif kind == (DEBIT, CASHFLOW_SOURCE):
            lines.append(ProfitLossLine(OPERATING_EXPENSE, item.category, item.amount))
        elif kind == (DEBIT, REVENUE_SOURCE):  # the main account transfer fees
            lines.append(ProfitLossLine(OTHER_EXPENSE, item.category, item.amount))
        elif kind == (DEBIT, LOAN_SOURCE):
            pass  # principal + interest: split below from the loans, never counted as is
        elif kind == (CREDIT, LOG_SOURCE):
            lines.append(ProfitLossLine(NON_PL, MANUAL_IN_LINE, item.amount))
        elif kind == (DEBIT, LOG_SOURCE):
            lines.append(ProfitLossLine(NON_PL, MANUAL_OUT_LINE, item.amount))
        else:
            warnings.append(
                f"unclassified statement rows {item.direction} {item.source_table}/{item.category} "
                f"({item.amount}) are not in the P&L"
            )
    interest = sum((item.interest_repaid_in_period for item in loans), ZERO)
    principal = sum((item.principal_repaid_in_period for item in loans), ZERO)
    if interest:
        lines.append(ProfitLossLine(FINANCE_COST, LOAN_INTEREST_LINE, interest))
    if principal:
        lines.append(ProfitLossLine(NON_PL, LOAN_PRINCIPAL_LINE, principal))
    order = {section: index for index, section in enumerate(SECTIONS)}
    return ProfitLoss(sorted(lines, key=lambda item: (order[item.section], -item.amount, item.line)))


def check_consistency(figures: CutoffFigures) -> None:
    """Records a warning for every cross-check between the statement and the other tables that
    does not hold (normally none: they only fail if the statement is out of sync or was edited)."""
    statement_repayments = sum(
        (
            item.amount
            for item in figures.categories
            if (item.direction, item.source_table, item.category) == (DEBIT, LOAN_SOURCE, LOAN_REPAYMENT_CATEGORY)
        ),
        ZERO,
    )
    loan_repayments = sum(
        (item.principal_repaid_in_period + item.interest_repaid_in_period for item in figures.loans), ZERO
    )
    if statement_repayments != loan_repayments:
        figures.warnings.append(
            f"loan repayments of the month ({loan_repayments}) differ from the statement's "
            f"{LOAN_REPAYMENT_CATEGORY} rows ({statement_repayments})"
        )
    pl, cash = figures.profit_loss, figures.cash
    bridged = (
        pl.net_profit
        - pl.line(NON_PL, LOAN_PRINCIPAL_LINE)
        + pl.line(NON_PL, MANUAL_IN_LINE)
        - pl.line(NON_PL, MANUAL_OUT_LINE)
    )
    if bridged != cash.cash_in - cash.cash_out:
        figures.warnings.append(
            f"the P&L does not reconcile with the cash movement: net profit - loan principal + manual "
            f"adjustments = {bridged}, cash in - cash out = {cash.cash_in - cash.cash_out}"
        )


def compute_cutoff(db: Session, period: Period, assets: MonthEndAssets) -> CutoffFigures:
    """Every figure of the cut-off of ``period`` (read only), ``assets`` being the month-end
    operational data fetched beforehand."""
    warnings: list[str] = []
    categories = category_totals(db, period)
    loans = loan_positions(db, period)
    potential_revenue = value_assets(db, period, assets)
    figures = CutoffFigures(
        period=period,
        cash=cash_position(db, period),
        categories=categories,
        profit_loss=profit_and_loss(categories, loans, warnings),
        loans=loans,
        receivables=customer_receivables(db, period),
        potential_revenue=potential_revenue,
        warnings=warnings,
    )
    check_consistency(figures)
    figures.warnings.extend(potential_revenue.warnings)
    return figures


# --- Saving -------------------------------------------------------------------------------------


def save_figures(db: Session, cutoff: MonthlyCutoff, figures: CutoffFigures) -> None:
    cash, pl = figures.cash, figures.profit_loss
    owed = figures.loans_outstanding
    cutoff.warnings = list(figures.warnings)
    cutoff.cash_opening, cutoff.opening_balances_in_period = cash.opening, cash.opening_balances_in_period
    cutoff.cash_in, cutoff.cash_out, cutoff.cash_closing = cash.cash_in, cash.cash_out, cash.closing
    cutoff.opening_balances_total, cutoff.statement_rows = cash.opening_balances_total, cash.rows
    cutoff.revenue, cutoff.operating_expenses = pl.total(REVENUE_SECTION), pl.total(OPERATING_EXPENSE)
    cutoff.other_expenses, cutoff.finance_costs = pl.total(OTHER_EXPENSE), pl.total(FINANCE_COST)
    cutoff.net_profit = pl.net_profit
    cutoff.loans_outstanding = len(owed)
    cutoff.loan_principal_outstanding = sum((item.principal_outstanding for item in owed), ZERO)
    cutoff.loan_interest_outstanding = sum((item.interest_outstanding for item in owed), ZERO)
    cutoff.loan_overdue_amount = sum((item.overdue_amount for item in figures.loans), ZERO)
    cutoff.loan_principal_repaid = sum((item.principal_repaid_in_period for item in figures.loans), ZERO)
    cutoff.loan_interest_repaid = sum((item.interest_repaid_in_period for item in figures.loans), ZERO)
    cutoff.receivable_invoices = sum((item.invoices for item in figures.receivables), 0)
    cutoff.receivables = sum((item.outstanding for item in figures.receivables), ZERO)
    cutoff.receivables_pending_verification = sum((item.pending_verification for item in figures.receivables), ZERO)
    potential = figures.potential_revenue
    cutoff.potential_revenue = potential.total()
    cutoff.potential_revenue_eggs = potential.total(EGG_STOCK)
    cutoff.potential_revenue_fish = potential.total(FISH_STOCK)
    cutoff.month_end_sources = dict(potential.sources)
    db.flush()
    db.add_all(
        MonthlyCutoffCategory(
            cutoff_id=cutoff.id,
            direction=item.direction,
            source_table=item.source_table,
            category=item.category,
            rows=item.rows,
            amount=item.amount,
        )
        for item in figures.categories
    )
    db.add_all(
        MonthlyCutoffProfitLoss(
            cutoff_id=cutoff.id, position=position, section=item.section, line=item.line, amount=item.amount
        )
        for position, item in enumerate(pl.lines, start=1)
    )
    db.add_all(
        MonthlyCutoffLoan(
            cutoff_id=cutoff.id,
            loan_id=item.loan_id,
            lender_id=item.lender_id,
            lender_name=item.lender_name,
            reference_number=item.reference_number,
            loan_type=item.loan_type,
            start_date=item.start_date,
            maturity_date=item.maturity_date,
            principal_amount=item.principal_amount,
            principal_paid=item.principal_paid,
            principal_outstanding=item.principal_outstanding,
            interest_total=item.interest_total,
            interest_paid=item.interest_paid,
            interest_outstanding=item.interest_outstanding,
            overdue_installments=item.overdue_installments,
            overdue_amount=item.overdue_amount,
            principal_repaid_in_period=item.principal_repaid_in_period,
            interest_repaid_in_period=item.interest_repaid_in_period,
        )
        for item in figures.loans
    )
    db.add_all(
        MonthlyCutoffReceivable(
            cutoff_id=cutoff.id,
            customer_id=item.customer_id,
            customer_name=item.customer_name,
            invoices=item.invoices,
            outstanding=item.outstanding,
            aging_0_30=item.aging_0_30,
            aging_31_60=item.aging_31_60,
            aging_61_90=item.aging_61_90,
            aging_over_90=item.aging_over_90,
            pending_verification=item.pending_verification,
        )
        for item in figures.receivables
    )
    db.add_all(
        MonthlyCutoffAsset(
            cutoff_id=cutoff.id,
            position=position,
            asset_type=item.asset_type,
            item_code=item.item_code,
            item_name=item.item_name,
            source_id=item.source_id,
            quantity_pieces=item.quantity_pieces,
            weight_kg=item.weight_kg,
            price_per_kg=item.price.price_per_kg if item.price else None,
            price_month=item.price.month.start if item.price else None,
            price_sales=item.price.sales if item.price else 0,
            value=item.value,
        )
        for position, item in enumerate(potential.lines, start=1)
    )


def mark_latest(db: Session, cutoff: MonthlyCutoff) -> None:
    """Makes ``cutoff`` the reported snapshot of its month (the previous one stays, unmarked)."""
    db.execute(
        update(MonthlyCutoff)
        .where(
            MonthlyCutoff.period_start == cutoff.period_start,
            MonthlyCutoff.is_latest.is_(True),
            MonthlyCutoff.id != cutoff.id,
        )
        .values(is_latest=False)
    )
    db.flush()  # before setting the new one: at most one latest per month (unique index)
    cutoff.is_latest = True


# --- Run ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CutoffOutcome:
    cutoff_id: int | None  # None for a dry run
    sync_run_id: int
    figures: CutoffFigures
    dry_run: bool


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    if session is not None:
        yield session
    else:
        with SessionLocal() as owned:
            yield owned


def cutoff_period(started_at: datetime, period: Period | None = None) -> Period:
    """The month to close: ``period``, or by default the month of the start time minus 30 minutes
    (like the nightly sync), i.e. the month that just ended for the run at 00:00 on the 1st. A
    month that has not ended at ``started_at`` cannot be closed."""
    period = period or nightly_period(started_at)
    if period.ends_at > started_at:
        raise MonthNotClosedError(f"{period.label} has not ended yet; a cut-off needs a closed month")
    return period


def last_ended_period(at: datetime) -> Period:
    """The latest month that has ended at ``at`` (the month before the current one, in
    ``APP_TIMEZONE``): what a run retrying until the month is closed works on."""
    return Period.month_of(Period.month_of(at.astimezone(settings.timezone).date()).start - timedelta(days=1))


def is_closed(period: Period, session: Session | None = None) -> bool:
    """Whether ``period`` has a reported (successful) cut-off."""
    with _session(session) as db:
        return bool(
            db.scalar(
                select(MonthlyCutoff.id).where(
                    MonthlyCutoff.period_start == period.start, MonthlyCutoff.is_latest.is_(True)
                )
            )
        )


def run_cutoff(*, period: Period | None = None, dry_run: bool = False, session: Session | None = None) -> CutoffOutcome:
    """Closes a month: fetches its month-end operational data, syncs its cash statement, computes
    the figures and saves them as a new snapshot marked ``is_latest``, recording a failed run too
    (then re-raises its error).

    The operational data is fetched first, before anything is recorded or synced: while it is not
    available (``MonthEndDataNotReadyError``) the month simply stays open, and an operational
    failure (unreachable, invalid data) fails the run before it starts.

    A dry run computes and returns the figures without saving anything; the month's cash
    statement sync still runs (and is recorded) for real, as it would on any night."""
    started_at = now_utc()
    period = cutoff_period(started_at, period)
    assets = fetch_month_end_assets(period)
    with _session(session) as db:
        if dry_run:
            sync = run_sync(period=period, session=db)
            try:
                lock_balances(db)  # a consistent picture: money writes wait for the read
                return CutoffOutcome(None, sync.run_id, compute_cutoff(db, period, assets), True)
            finally:
                db.rollback()

        cutoff = MonthlyCutoff(
            period_start=period.start, period_end=period.end, status=RUNNING, started_at=started_at, warnings=[]
        )
        db.add(cutoff)
        db.commit()
        sync_run_id: int | None = None
        try:
            sync_run_id = run_sync(period=period, session=db).run_id
            lock_balances(db)
            figures = compute_cutoff(db, period, assets)
            cutoff.sync_run_id = sync_run_id
            save_figures(db, cutoff, figures)
            mark_latest(db, cutoff)
            cutoff.status, cutoff.finished_at = SUCCESS, now_utc()
            db.commit()
        except Exception as exc:
            try:
                db.rollback()
                cutoff.status, cutoff.finished_at = FAILED, now_utc()
                cutoff.sync_run_id, cutoff.error = sync_run_id, f"{type(exc).__name__}: {exc}"
                db.commit()
            except Exception:
                logger.exception("could not record monthly cut-off %s as failed", cutoff.id)
            raise
        for warning in figures.warnings:
            logger.warning("monthly cut-off %s (%s): %s", cutoff.id, period.label, warning)
        return CutoffOutcome(cutoff.id, sync_run_id, figures, False)
