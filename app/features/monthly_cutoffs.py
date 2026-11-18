"""Read API of the monthly cut-off (see ``monthly_cutoff_service`` for how it is computed).

One endpoint, one month per call: ``GET /monthly-cutoffs?year=YYYY&month=M`` returns the month's
reported snapshot (``is_latest``) with all of its figures. Both filters are required and may be
given once only; there is deliberately no range or list form. Readable by the ``ADMIN`` and
``SUPERADMIN`` roles.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.decimal import decimal_string
from app.core.dependencies import DbSession, FinanceReader
from app.core.errors import AppError, validation
from app.db.models import (
    MonthlyCutoff,
    MonthlyCutoffCategory,
    MonthlyCutoffLoan,
    MonthlyCutoffProfitLoss,
    MonthlyCutoffReceivable,
)
from app.features.cash_statement_service import Period
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
        "period": {
            "year": cutoff.period_start.year,
            "month": cutoff.period_start.month,
            "start": iso(cutoff.period_start),
            "end": iso(cutoff.period_end),
        },
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
    loans, receivables and the cash statement grouped by category, as of the month's last day.

    Returns the month's reported snapshot (the latest successful run). ``404`` when the month has
    not been closed (yet)."""
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
