"""Read API of the monthly cut-off (see ``monthly_cutoff_service`` for how it is computed).

- ``GET /monthly-cutoffs/months``: the list, one row per month from the first one with data to the
  current one, newest first, closed or not: the status of its cut-off, headline figures and the
  status of its Excel export. Only what a list needs.
- ``GET /monthly-cutoffs?year=YYYY&month=M``: one month's reported snapshot (``is_latest``) with all
  of its figures, for the detail. Both filters are required and may be given once only.

Creating a cut-off or its Excel file from the frontend is a background job
(``app.features.monthly_cutoff_jobs``); the Excel download is ``app.features.monthly_cutoff_export``.
Readable by the ``ADMIN`` and ``SUPERADMIN`` roles.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.decimal import decimal_string
from app.core.dependencies import DbSession, FinanceReader
from app.core.errors import AppError, validation
from app.core.pagination import PageDep
from app.db.models import (
    MonthlyCutoff,
    MonthlyCutoffAsset,
    MonthlyCutoffCategory,
    MonthlyCutoffExport,
    MonthlyCutoffJob,
    MonthlyCutoffLoan,
    MonthlyCutoffProfitLoss,
    MonthlyCutoffReceivable,
    OperationalCashflow,
)
from app.features.cash_statement_service import Period
from app.features.common import now_utc, today_local
from app.features.month_end_assets import (
    EGG_STOCK,
    EGG_STOCK_DATASET,
    FISH_ESTIMATE_DATASET,
    FISH_STOCK,
    VALUATION,
)
from app.features.monthly_cutoff_export_state import export_out
from app.features.monthly_cutoff_job_state import CUTOFF_JOB, EXPORT_JOB, cutoff_status, job_out, user_names
from app.features.monthly_cutoff_service import SUCCESS

router = APIRouter(tags=["Monthly Cut-off"])

MIN_YEAR, MAX_YEAR = 2000, 2100


def amount(value: Decimal | int) -> str:
    return decimal_string(value, 0) or "0"


def iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def single_value(request: Request, *names: str) -> None:
    """Rejects a filter given more than once (``?month=7&month=8``): one call, one month."""
    for name in names:
        if len(request.query_params.getlist(name)) > 1:
            raise validation(name, "Give a single value: one call returns one month.")


def weight(value: Decimal | int) -> str:
    return decimal_string(value, 3) or "0.000"


def price_out(item: MonthlyCutoffAsset) -> dict[str, object] | None:
    """The sales price an asset line was valued at; ``None``: not priced (no sale to price it from)."""
    if item.price_per_kg is None or item.price_month is None:
        return None
    return {
        "price_per_kg": decimal_string(item.price_per_kg, 3),
        "month": f"{item.price_month:%Y-%m}",
        "sales": item.price_sales,
    }


def potential_revenue_out(cutoff: MonthlyCutoff, assets: list[MonthlyCutoffAsset]) -> dict[str, object] | None:
    """Month-end egg stock and fish weight estimate valued at sales prices. ``None`` for a
    snapshot taken before the cut-off included it."""
    if cutoff.potential_revenue is None:
        return None
    sources = cutoff.month_end_sources or {}
    eggs = [item for item in assets if item.asset_type == EGG_STOCK]
    ponds = [item for item in assets if item.asset_type == FISH_STOCK]
    counted = [item.quantity_pieces for item in ponds if item.quantity_pieces is not None]
    return {
        "valuation": VALUATION,
        "total": amount(cutoff.potential_revenue),
        "eggs": {
            "value": amount(cutoff.potential_revenue_eggs or 0),
            "weight_kg": weight(sum((item.weight_kg for item in eggs), Decimal("0"))),
            "quantity_pieces": sum((item.quantity_pieces or 0 for item in eggs), 0),
            "source": sources.get(EGG_STOCK_DATASET),
            "items": [
                {
                    "egg_category": item.item_code,
                    "weight_kg": weight(item.weight_kg),
                    "quantity_pieces": item.quantity_pieces,
                    "price": price_out(item),
                    "value": amount(item.value),
                }
                for item in eggs
            ],
        },
        "fish": {
            "value": amount(cutoff.potential_revenue_fish or 0),
            "weight_kg": weight(sum((item.weight_kg for item in ponds), Decimal("0"))),
            # Only when operational gave the count of every pond.
            "fish_count": sum(counted, 0) if len(counted) == len(ponds) else None,
            "source": sources.get(FISH_ESTIMATE_DATASET),
            "ponds": [
                {
                    "pond": {"id": item.source_id, "code": item.item_code, "name": item.item_name},
                    "weight_kg": weight(item.weight_kg),
                    "fish_count": item.quantity_pieces,
                    "price": price_out(item),
                    "value": amount(item.value),
                }
                for item in ponds
            ],
        },
    }


def period_out(cutoff: MonthlyCutoff) -> dict[str, object]:
    return {
        "year": cutoff.period_start.year,
        "month": cutoff.period_start.month,
        "start": iso(cutoff.period_start),
        "end": iso(cutoff.period_end),
    }


def revision_of(cutoff: Any) -> Any:
    """How many times the month had been closed (successfully) when ``cutoff`` was taken; > 1 means
    it was restated after late entries. ``cutoff``: the model or an alias of it (in a query)."""
    earlier = aliased(MonthlyCutoff)
    return (
        select(func.count())
        .where(
            earlier.period_start == cutoff.period_start,
            earlier.status == SUCCESS,
            earlier.started_at <= cutoff.started_at,
        )
        .scalar_subquery()
    )


def export_of(db: Session, cutoff: MonthlyCutoff) -> dict[str, object]:
    row = db.scalar(select(MonthlyCutoffExport).where(MonthlyCutoffExport.cutoff_id == cutoff.id))
    job = latest_jobs(db, [cutoff.period_start]).get((EXPORT_JOB, cutoff.period_start))
    names = user_names(db, [row.attempt_requested_by if row else None])
    return export_out(cutoff, row, now_utc(), names, job)


def cutoff_out(db: Session, cutoff: MonthlyCutoff) -> dict[str, object]:
    def rows(model: Any) -> list[Any]:
        return list(db.scalars(select(model).where(model.cutoff_id == cutoff.id).order_by(model.id)).all())

    revision = db.scalar(
        select(func.count()).where(
            MonthlyCutoff.period_start == cutoff.period_start,
            MonthlyCutoff.status == SUCCESS,
            MonthlyCutoff.started_at <= cutoff.started_at,
        )
    )
    profit_loss = sorted(rows(MonthlyCutoffProfitLoss), key=lambda item: item.position)
    return {
        "id": cutoff.id,
        "period": period_out(cutoff),
        # How many times the month has been closed; > 1 means it was restated after late entries.
        "revision": revision,
        "generated_at": cutoff.finished_at.isoformat() if cutoff.finished_at else None,
        "cash_statement_sync_run_id": cutoff.sync_run_id,
        "warnings": list(cutoff.warnings or []),
        "cash": {
            "opening": amount(cutoff.cash_opening),
            "opening_balances_in_period": amount(cutoff.opening_balances_in_period),
            "cash_in": amount(cutoff.cash_in),
            "cash_out": amount(cutoff.cash_out),
            "closing": amount(cutoff.cash_closing),
            "opening_balances_total": amount(cutoff.opening_balances_total),
            "statement_rows": cutoff.statement_rows,
        },
        "profit_and_loss": {
            "basis": "CASH",
            "revenue": amount(cutoff.revenue),
            "operating_expenses": amount(cutoff.operating_expenses),
            "other_expenses": amount(cutoff.other_expenses),
            "finance_costs": amount(cutoff.finance_costs),
            "net_profit": amount(cutoff.net_profit),
            "lines": [
                {"section": item.section, "line": item.line, "amount": amount(item.amount)} for item in profit_loss
            ],
        },
        "loans": {
            "outstanding_count": cutoff.loans_outstanding,
            "principal_outstanding": amount(cutoff.loan_principal_outstanding),
            "interest_outstanding": amount(cutoff.loan_interest_outstanding),
            "overdue_amount": amount(cutoff.loan_overdue_amount),
            "principal_repaid": amount(cutoff.loan_principal_repaid),
            "interest_repaid": amount(cutoff.loan_interest_repaid),
            "items": [
                {
                    "loan_id": item.loan_id,
                    "lender": {"id": item.lender_id, "name": item.lender_name},
                    "reference_number": item.reference_number,
                    "loan_type": item.loan_type,
                    "start_date": iso(item.start_date),
                    "maturity_date": iso(item.maturity_date),
                    "principal_amount": amount(item.principal_amount),
                    "principal_paid": amount(item.principal_paid),
                    "principal_outstanding": amount(item.principal_outstanding),
                    "interest_total": amount(item.interest_total),
                    "interest_paid": amount(item.interest_paid),
                    "interest_outstanding": amount(item.interest_outstanding),
                    "overdue_installments": item.overdue_installments,
                    "overdue_amount": amount(item.overdue_amount),
                    "principal_repaid_in_period": amount(item.principal_repaid_in_period),
                    "interest_repaid_in_period": amount(item.interest_repaid_in_period),
                }
                for item in rows(MonthlyCutoffLoan)
            ],
        },
        "receivables": {
            "invoices": cutoff.receivable_invoices,
            "outstanding": amount(cutoff.receivables),
            "pending_verification": amount(cutoff.receivables_pending_verification),
            "customers": [
                {
                    "customer": {"id": item.customer_id, "name": item.customer_name},
                    "invoices": item.invoices,
                    "outstanding": amount(item.outstanding),
                    "aging": {
                        "days_0_30": amount(item.aging_0_30),
                        "days_31_60": amount(item.aging_31_60),
                        "days_61_90": amount(item.aging_61_90),
                        "over_90_days": amount(item.aging_over_90),
                    },
                    "pending_verification": amount(item.pending_verification),
                }
                for item in rows(MonthlyCutoffReceivable)
            ],
        },
        "categories": [
            {
                "direction": item.direction,
                "source_table": item.source_table,
                "category": item.category,
                "rows": item.rows,
                "amount": amount(item.amount),
            }
            for item in rows(MonthlyCutoffCategory)
        ],
        "potential_revenue": potential_revenue_out(
            cutoff, sorted(rows(MonthlyCutoffAsset), key=lambda item: item.position)
        ),
        "export": export_of(db, cutoff),
    }


def months_listed(db: Session, year: int | None) -> list[Period]:
    """Every month from the first one with data (the earliest ``operational_cashflows`` transaction,
    or an earlier cut-off or job) to the current month, newest first."""
    firsts = [
        db.scalar(select(func.min(OperationalCashflow.transaction_date))),
        db.scalar(select(func.min(MonthlyCutoff.period_start))),
        db.scalar(select(func.min(MonthlyCutoffJob.period_start))),
    ]
    known = [item for item in firsts if item is not None]
    if not known:
        return []
    first, current = Period.month_of(min(known)), Period.month_of(today_local())
    months: list[Period] = []
    period = current
    while period.start >= first.start:
        if year is None or period.start.year == year:
            months.append(period)
        period = Period.month_of(period.start - timedelta(days=1))
    return months


def latest_jobs(db: Session, starts: list[date]) -> dict[tuple[str, date], MonthlyCutoffJob]:
    """The latest job of each type of each month."""
    jobs = db.scalars(
        select(MonthlyCutoffJob)
        .where(MonthlyCutoffJob.period_start.in_(starts))
        .order_by(
            MonthlyCutoffJob.job_type,
            MonthlyCutoffJob.period_start,
            MonthlyCutoffJob.requested_at.desc(),
            MonthlyCutoffJob.id.desc(),
        )
        .distinct(MonthlyCutoffJob.job_type, MonthlyCutoffJob.period_start)
    ).all()
    return {(job.job_type, job.period_start): job for job in jobs}


NO_FIGURES: dict[str, object] = dict.fromkeys(
    ("cash_closing", "revenue", "net_profit", "receivables", "loan_principal_outstanding", "potential_revenue")
)


def figures_out(cutoff: MonthlyCutoff) -> dict[str, object]:
    return {
        "cash_closing": amount(cutoff.cash_closing),
        "revenue": amount(cutoff.revenue),
        "net_profit": amount(cutoff.net_profit),
        "receivables": amount(cutoff.receivables),
        "loan_principal_outstanding": amount(cutoff.loan_principal_outstanding),
        # None: closed before potential revenue was part of the cut-off.
        "potential_revenue": None if cutoff.potential_revenue is None else amount(cutoff.potential_revenue),
    }


def list_item_out(
    period: Period,
    found: tuple[MonthlyCutoff, int, MonthlyCutoffExport | None] | None,
    jobs: dict[tuple[str, date], MonthlyCutoffJob],
    now: Any,
    names: dict[int, str],
) -> dict[str, object]:
    cutoff, revision, export = found if found else (None, None, None)
    cutoff_job = jobs.get((CUTOFF_JOB, period.start))
    export_job = jobs.get((EXPORT_JOB, period.start))
    return {
        "period": {
            "year": period.start.year,
            "month": period.start.month,
            "start": iso(period.start),
            "end": iso(period.end),
        },
        "cutoff": {
            **cutoff_status(period, cutoff, cutoff_job, now),
            "id": cutoff.id if cutoff else None,
            "revision": revision,
            "generated_at": cutoff.finished_at.isoformat() if cutoff and cutoff.finished_at else None,
            "warning_count": len(cutoff.warnings or []) if cutoff else None,
            "last_job": job_out(cutoff_job, names, now) if cutoff_job else None,
        },
        # Every value None when the month has no cut-off (shown as "-").
        "figures": figures_out(cutoff) if cutoff else dict(NO_FIGURES),
        # None when the month has no cut-off: nothing to export.
        "export": export_out(cutoff, export, now, names, export_job) if cutoff else None,
    }


@router.get("/monthly-cutoffs/months")
def list_monthly_cutoffs(
    _: FinanceReader,
    db: DbSession,
    page: PageDep,
    year: int | None = Query(default=None, ge=MIN_YEAR, le=MAX_YEAR, description="Only the months of this year."),
) -> dict[str, object]:
    """Every month, newest first, from the first one with data (the earliest operational cashflow)
    to the current one, whether its cut-off exists or not: ``cutoff.status`` (``NOT_ENDED``,
    ``NOT_CREATED``, ``QUEUED``, ``RUNNING``, ``WAITING_FOR_DATA``, ``FAILED`` or ``CREATED``, with
    ``reason``), a few headline figures (``null`` without a cut-off) and the status of its Excel
    export (``null`` without a cut-off).

    ``cutoff.can_generate`` / ``export.can_generate``: whether ``POST /monthly-cutoffs/jobs`` may
    queue a ``CUTOFF`` / ``EXPORT`` job for the month now. Only what a list needs: a month's full
    figures come from ``GET /monthly-cutoffs?year=&month=``."""
    months = months_listed(db, year)
    shown = months[page.offset : page.offset + page.per_page]
    starts = [period.start for period in shown]
    cutoff = aliased(MonthlyCutoff)
    rows = (
        db.execute(
            select(cutoff, revision_of(cutoff), MonthlyCutoffExport)
            .outerjoin(MonthlyCutoffExport, MonthlyCutoffExport.cutoff_id == cutoff.id)
            .where(cutoff.is_latest.is_(True), cutoff.period_start.in_(starts))
        ).all()
        if starts
        else []
    )
    found = {item.period_start: (item, int(revision), export) for item, revision, export in rows}
    jobs = latest_jobs(db, starts) if starts else {}
    names = user_names(
        db,
        [job.requested_by for job in jobs.values()]
        + [export.attempt_requested_by for _, _, export in found.values() if export is not None],
    )
    now = now_utc()
    return {
        "data": [list_item_out(period, found.get(period.start), jobs, now, names) for period in shown],
        "meta": page.meta(len(months)),
    }


@router.get("/monthly-cutoffs")
def get_monthly_cutoff(
    request: Request,
    _: FinanceReader,
    db: DbSession,
    year: int = Query(ge=MIN_YEAR, le=MAX_YEAR, description="Required, e.g. 2026."),
    month: int = Query(ge=1, le=12, description="Required, 1-12."),
) -> dict[str, object]:
    """The month-end close of one month: actual cash, profit and loss (cash basis), outstanding
    loans, receivables, the cash statement grouped by category and the potential revenue of the
    month-end stock (eggs, fish; not cash and not in the P&L), as of the month's last day.

    Returns the month's reported snapshot (the latest successful run). ``404`` when the month has
    not been closed (yet): a month is closed only once operational-service has published its
    month-end egg stock and fish weight estimate."""
    single_value(request, "year", "month")
    period = Period(date(year, month, 1))
    cutoff = db.scalar(
        select(MonthlyCutoff).where(MonthlyCutoff.period_start == period.start, MonthlyCutoff.is_latest.is_(True))
    )
    if cutoff is None:
        raise AppError(
            404, "NOT_FOUND", f"The monthly cut-off of {period.label} was not found: the month has not been closed."
        )
    return {"data": cutoff_out(db, cutoff)}
