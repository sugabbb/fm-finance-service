"""Lender management API (see ``lender_service``)."""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Query, status
from pydantic import EmailStr, Field
from sqlalchemy import exists, func, or_, select

from app.core.decimal import decimal_string
from app.core.dependencies import ActiveUser, DbSession, Superadmin
from app.core.errors import not_found
from app.core.pagination import PageDep, StrictModel
from app.db.models import Lender, Loan
from app.features.lender_service import LenderExposure, create_lender, lender_exposures, update_lender
from app.features.loan_service import ACTIVE, LoanSummary, installment_outstanding

router = APIRouter(tags=["Lenders"])


class LenderInput(StrictModel):
    name: str = Field(min_length=1, max_length=255, description="Required and unique (case-insensitive).")
    contact_person: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=3000)
    notes: str | None = Field(default=None, max_length=3000)
    is_active: bool = Field(default=True, description="An inactive lender cannot receive new loans.")


def money_out(value: Decimal | int | None) -> str | None:
    return decimal_string(value, 0)


def date_out(value: date | None) -> str | None:
    return value.isoformat() if value else None


def active_loan_out(loan: Loan, summary: LoanSummary) -> dict[str, object]:
    next_installment = summary.next_installment
    return {
        "id": loan.id,
        "reference_number": loan.reference_number,
        "loan_type": loan.loan_type,
        "start_date": loan.start_date.isoformat(),
        "maturity_date": date_out(summary.maturity_date),
        "principal_amount": money_out(loan.principal_amount),
        "total_interest": money_out(summary.total_interest),
        "total_payable": money_out(Decimal(loan.principal_amount) + summary.total_interest),
        "total_paid": money_out(summary.principal_paid + summary.interest_paid),
        "principal_outstanding": money_out(summary.principal_outstanding),
        "interest_outstanding": money_out(summary.interest_outstanding),
        "total_outstanding": money_out(summary.total_outstanding),
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
    }


def lender_out(lender: Lender, exposure: LenderExposure) -> dict[str, object]:
    return {
        "id": lender.id,
        "name": lender.name,
        "contact_person": lender.contact_person,
        "phone": lender.phone,
        "email": lender.email,
        "address": lender.address,
        "notes": lender.notes,
        "is_active": lender.is_active,
        "created_at": lender.created_at.isoformat(),
        "updated_at": lender.updated_at.isoformat(),
        # Totals of the active loans: full principal + interest, repaid, and still to repay.
        "active_loans_summary": {
            "active_loans": len(exposure.loans),
            "principal_amount": money_out(exposure.principal_amount),
            "total_interest": money_out(exposure.total_interest),
            "total_payable": money_out(exposure.total_payable),
            "total_paid": money_out(exposure.total_paid),
            "principal_outstanding": money_out(exposure.principal_outstanding),
            "interest_outstanding": money_out(exposure.interest_outstanding),
            "total_outstanding": money_out(exposure.total_outstanding),
            "overdue_amount": money_out(exposure.overdue_amount),
            "next_due_date": date_out(exposure.next_due_date),
        },
        "active_loans": [active_loan_out(loan, summary) for loan, summary in exposure.loans],
    }


@router.get("/lenders")
def list_lenders(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    search: str | None = Query(default=None, description="Case-insensitive part of the name or contact person."),
    is_active: bool | None = None,
    has_active_loans: bool | None = None,
) -> dict[str, object]:
    """Lenders by name, each with its active loans and what is still to be repaid."""
    stmt = select(Lender)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(Lender.name.ilike(pattern), Lender.contact_person.ilike(pattern)))
    if is_active is not None:
        stmt = stmt.where(Lender.is_active.is_(is_active))
    if has_active_loans is not None:
        active = exists().where(Loan.lender_id == Lender.id, Loan.status == ACTIVE)
        stmt = stmt.where(active if has_active_loans else ~active)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    lenders = db.scalars(
        stmt.order_by(func.lower(Lender.name), Lender.id).offset(page.offset).limit(page.per_page)
    ).all()
    exposures = lender_exposures(db, [lender.id for lender in lenders])
    return {"data": [lender_out(lender, exposures[lender.id]) for lender in lenders], "meta": page.meta(total)}


@router.get("/lenders/{lender_id}")
def get_lender(lender_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    lender = db.get(Lender, lender_id)
    if lender is None:
        raise not_found("Lender")
    return {"data": lender_out(lender, lender_exposures(db, [lender.id])[lender.id])}


@router.post("/lenders", status_code=status.HTTP_201_CREATED)
def create_lender_endpoint(payload: LenderInput, actor: Superadmin, db: DbSession) -> dict[str, object]:
    lender = create_lender(db, payload.model_dump(), actor)
    db.commit()
    return get_lender(lender.id, actor, db)


@router.patch("/lenders/{lender_id}")
def update_lender_endpoint(lender_id: int, payload: LenderInput, actor: Superadmin, db: DbSession) -> dict[str, object]:
    update_lender(db, lender_id, payload.model_dump(), actor)
    db.commit()
    return get_lender(lender_id, actor, db)
