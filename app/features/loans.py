"""Loan management API (see ``loan_service`` for the rules)."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query, status
from pydantic import Field
from sqlalchemy import func, select

from app.core.decimal import decimal_string
from app.core.dependencies import ActiveUser, DbSession, Superadmin
from app.core.pagination import PageDep, StrictModel
from app.db.models import Loan, LoanInstallment, LoanRepayment
from app.features.common import actor_name, today_local
from app.features.finance import audits
from app.features.loan_service import (
    ACTIVE,
    LOAN_TYPES,
    ZERO,
    cancel_loan,
    create_loan,
    get_loan,
    installment_outstanding,
    is_overdue,
    loan_query,
    repay_loan,
    summarize,
    update_loan,
    void_repayment,
)
from app.features.main_cash_account_events import drain_in_background
from app.features.main_cash_account_service import LOAN_REPAYMENT_SOURCE, source_reference

router = APIRouter(tags=["Loans"])

LoanType = Literal["SHORT_TERM", "LONG_TERM"]
LoanStatus = Literal["ACTIVE", "PAID_OFF", "CANCELLED"]


class LoanBase(StrictModel):
    lender_id: int = Field(description="An active lender (see /lenders).")
    reference_number: str | None = Field(default=None, max_length=100, description="Contract/account number.")
    principal_amount: Decimal = Field(gt=0, description="Whole IDR.")
    start_date: date = Field(description="Date the loan was received/signed.")
    notes: str | None = Field(default=None, max_length=3000)


class ShortTermLoanInput(LoanBase):
    """One repayment of principal + a flat interest amount on a chosen date."""

    loan_type: Literal["SHORT_TERM"]
    interest_amount: Decimal = Field(
        ge=0, description="Flat interest in whole IDR (0 allowed). For a rate, enter the resulting amount."
    )
    due_date: date = Field(description="The single repayment date: any day from start_date (e.g. one week later).")


class LongTermLoanInput(LoanBase):
    """Monthly installments with a fixed annual interest rate."""

    loan_type: Literal["LONG_TERM"]
    annual_interest_rate: Decimal = Field(ge=0, le=100, decimal_places=4, description="Percent per year, e.g. 11.5.")
    interest_method: Literal["FLAT", "ANNUITY"] = Field(
        description="FLAT: interest on the original principal every month. "
        "ANNUITY: equal monthly installments on the declining balance."
    )
    term_months: int = Field(ge=1, le=600, description="Number of monthly installments.")
    first_due_date: date = Field(description="Due date of the first installment; the next ones follow monthly.")


LoanInput = Annotated[ShortTermLoanInput | LongTermLoanInput, Field(discriminator="loan_type")]


class RepaymentInput(StrictModel):
    paid_date: date
    amount: Decimal | None = Field(
        default=None, gt=0, description="Whole IDR. Omit to pay what is still due on the next installment."
    )
    notes: str | None = Field(default=None, max_length=3000)


class ReasonInput(StrictModel):
    reason: str = Field(min_length=1, max_length=3000)


def money_out(value: Decimal | int | None) -> str | None:
    return decimal_string(value, 0)


def installment_out(item: LoanInstallment, as_of: date) -> dict[str, object]:
    return {
        "id": item.id,
        "sequence": item.sequence,
        "due_date": item.due_date.isoformat(),
        "principal_amount": money_out(item.principal_amount),
        "interest_amount": money_out(item.interest_amount),
        "total_amount": money_out(Decimal(item.principal_amount) + Decimal(item.interest_amount)),
        "principal_paid": money_out(item.principal_paid),
        "interest_paid": money_out(item.interest_paid),
        "outstanding_amount": money_out(installment_outstanding(item)),
        "status": item.status,
        "paid_date": item.paid_date.isoformat() if item.paid_date else None,
        "is_overdue": is_overdue(item, as_of),
    }


def repayment_out(db: DbSession, item: LoanRepayment) -> dict[str, object]:
    return {
        "id": item.id,
        "paid_date": item.paid_date.isoformat(),
        "amount": money_out(item.amount),
        "principal_amount": money_out(item.principal_amount),
        "interest_amount": money_out(item.interest_amount),
        "first_installment": item.first_installment,
        "last_installment": item.last_installment,
        "notes": item.notes,
        "main_cash_account_reference": source_reference(LOAN_REPAYMENT_SOURCE, item.id, "loan_repaid"),
        "created_by": item.created_by,
        "creator_name": actor_name(db, item.created_by),
        "created_at": item.created_at.isoformat(),
        "voided_at": item.voided_at.isoformat() if item.voided_at else None,
        "voided_by": item.voided_by,
        "voider_name": actor_name(db, item.voided_by),
        "void_reason": item.void_reason,
    }


def loan_out(db: DbSession, loan: Loan, *, detail: bool = False) -> dict[str, object]:
    as_of = today_local()
    summary = summarize(loan, as_of)
    next_installment = summary.next_installment
    data: dict[str, object] = {
        "id": loan.id,
        "lender": {"id": loan.lender.id, "name": loan.lender.name},
        "reference_number": loan.reference_number,
        "loan_type": loan.loan_type,
        "principal_amount": money_out(loan.principal_amount),
        # SHORT_TERM: flat interest amount; LONG_TERM: rate, method and number of months.
        "interest_amount": money_out(loan.interest_amount),
        "annual_interest_rate": decimal_string(loan.annual_interest_rate, 4),
        "interest_method": loan.interest_method,
        "term_months": loan.term_months,
        "start_date": loan.start_date.isoformat(),
        # The (first) installment's due date: for SHORT_TERM, its single repayment date.
        "first_due_date": loan.first_due_date.isoformat(),
        "maturity_date": summary.maturity_date.isoformat() if summary.maturity_date else None,
        "status": loan.status,
        "notes": loan.notes,
        "cancelled_at": loan.cancelled_at.isoformat() if loan.cancelled_at else None,
        "cancel_reason": loan.cancel_reason,
        "created_by": loan.created_by,
        "created_at": loan.created_at.isoformat(),
        "updated_at": loan.updated_at.isoformat(),
        "summary": {
            "total_interest": money_out(summary.total_interest),
            "total_payable": money_out(Decimal(loan.principal_amount) + summary.total_interest),
            "principal_paid": money_out(summary.principal_paid),
            "interest_paid": money_out(summary.interest_paid),
            "total_paid": money_out(summary.principal_paid + summary.interest_paid),
            "principal_outstanding": money_out(summary.principal_outstanding),
            "interest_outstanding": money_out(summary.interest_outstanding),
            "total_outstanding": money_out(summary.total_outstanding),
            "installments_total": summary.installments_total,
            "installments_paid": summary.installments_paid,
            "overdue_installments": summary.overdue_installments,
            "overdue_amount": money_out(summary.overdue_amount),
            "next_installment": (
                {
                    "sequence": next_installment.sequence,
                    "due_date": next_installment.due_date.isoformat(),
                    "amount_due": money_out(installment_outstanding(next_installment)),
                }
                if next_installment
                else None
            ),
        },
    }
    if detail:
        data["installments"] = [installment_out(item, as_of) for item in loan.installments]
        data["repayments"] = [repayment_out(db, item) for item in loan.repayments]
        data["audit_logs"] = audits(db, "loans", loan.id)
    return data


@router.get("/loans")
def list_loans(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    loan_type: LoanType | None = None,
    status_filter: LoanStatus | None = Query(default=None, alias="status"),
    lender_id: int | None = None,
) -> dict[str, object]:
    stmt = loan_query()
    if loan_type:
        stmt = stmt.where(Loan.loan_type == loan_type)
    if status_filter:
        stmt = stmt.where(Loan.status == status_filter)
    if lender_id is not None:
        stmt = stmt.where(Loan.lender_id == lender_id)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Loan.start_date.desc(), Loan.id.desc()).offset(page.offset).limit(page.per_page)
    ).all()
    return {"data": [loan_out(db, row) for row in rows], "meta": page.meta(total)}


@router.get("/loans/summary")
def loans_summary(_: ActiveUser, db: DbSession) -> dict[str, object]:
    """What the business still owes on its active loans, in total and per loan type."""
    loans = db.scalars(loan_query().where(Loan.status == ACTIVE)).all()
    as_of = today_local()

    def totals(selected: list[Loan]) -> dict[str, object]:
        summaries = [summarize(loan, as_of) for loan in selected]
        due_dates = [item.next_installment.due_date for item in summaries if item.next_installment]
        return {
            "active_loans": len(selected),
            "principal_amount": money_out(sum((Decimal(loan.principal_amount) for loan in selected), ZERO)),
            "principal_outstanding": money_out(sum((item.principal_outstanding for item in summaries), ZERO)),
            "interest_outstanding": money_out(sum((item.interest_outstanding for item in summaries), ZERO)),
            "total_outstanding": money_out(sum((item.total_outstanding for item in summaries), ZERO)),
            "overdue_installments": sum(item.overdue_installments for item in summaries),
            "overdue_amount": money_out(sum((item.overdue_amount for item in summaries), ZERO)),
            "next_due_date": min(due_dates).isoformat() if due_dates else None,
        }

    return {
        "data": {
            "as_of": as_of.isoformat(),
            "total": totals(list(loans)),
            "by_type": {
                loan_type: totals([loan for loan in loans if loan.loan_type == loan_type]) for loan_type in LOAN_TYPES
            },
        }
    }


@router.get("/loans/{loan_id}")
def get_loan_detail(loan_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    return {"data": loan_out(db, get_loan(db, loan_id), detail=True)}


@router.post("/loans", status_code=status.HTTP_201_CREATED)
def create_loan_endpoint(payload: LoanInput, actor: Superadmin, db: DbSession) -> dict[str, object]:
    loan = create_loan(db, payload.model_dump(), actor)
    db.commit()
    return get_loan_detail(loan.id, actor, db)


@router.patch("/loans/{loan_id}")
def update_loan_endpoint(loan_id: int, payload: LoanInput, actor: Superadmin, db: DbSession) -> dict[str, object]:
    update_loan(db, loan_id, payload.model_dump(), actor)
    db.commit()
    return get_loan_detail(loan_id, actor, db)


@router.post("/loans/{loan_id}/cancel")
def cancel_loan_endpoint(loan_id: int, payload: ReasonInput, actor: Superadmin, db: DbSession) -> dict[str, object]:
    cancel_loan(db, loan_id, actor, payload.reason)
    db.commit()
    return get_loan_detail(loan_id, actor, db)


@router.post("/loans/{loan_id}/repayments", status_code=status.HTTP_201_CREATED)
def repay_loan_endpoint(
    loan_id: int, payload: RepaymentInput, actor: Superadmin, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    repay_loan(db, loan_id, actor, payload.paid_date, payload.amount, payload.notes)
    db.commit()
    # The main cash account debit is posted after the response (see main_cash_account_events).
    background_tasks.add_task(drain_in_background)
    return get_loan_detail(loan_id, actor, db)


@router.patch("/loans/{loan_id}/repayments/{repayment_id}/void")
def void_repayment_endpoint(
    loan_id: int,
    repayment_id: int,
    payload: ReasonInput,
    actor: Superadmin,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    void_repayment(db, loan_id, repayment_id, actor, payload.reason)
    db.commit()
    background_tasks.add_task(drain_in_background)
    return get_loan_detail(loan_id, actor, db)
