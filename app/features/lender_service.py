"""Lender management: the banks, cooperatives or people the business borrows from.

Only the name is required (unique, case-insensitive); contact person, phone, email, address and
notes are optional. A lender is never deleted: deactivating it (``is_active`` false) keeps its
loans and history but prevents new loans from being recorded against it.

:func:`lender_exposures` rolls up the **active** loans of lenders with the same figures as the loan
module (:func:`app.features.loan_service.summarize`): the full principal + interest of each loan,
what has been repaid and what still has to be repaid.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import conflict, not_found, validation
from app.db.models import Lender, Loan, User
from app.features.common import audit, model_snapshot, today_local
from app.features.loan_service import ACTIVE, ZERO, LoanSummary, loan_query, summarize
from app.features.locks import lock_balances

OPTIONAL_FIELDS = ("contact_person", "phone", "email", "address", "notes")


# --- Writes -------------------------------------------------------------------------------------


def clean_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise validation("name", "Name is required.")
    return name


def assert_unique_name(db: Session, name: str, exclude_id: int | None = None) -> None:
    stmt = select(Lender.id).where(func.lower(Lender.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(Lender.id != exclude_id)
    if db.scalar(stmt) is not None:
        raise conflict("LENDER_EXISTS", f"A lender named {name!r} already exists.")


def apply_input(lender: Lender, data: dict[str, Any], name: str) -> None:
    lender.name = name
    for key in OPTIONAL_FIELDS:
        value = data.get(key)
        if isinstance(value, str):
            value = value.strip() or None  # blank optional fields are stored as NULL
        setattr(lender, key, value)
    lender.is_active = data.get("is_active", True)


def create_lender(db: Session, data: dict[str, Any], actor: User) -> Lender:
    name = clean_name(data["name"])
    assert_unique_name(db, name)
    lender = Lender()
    apply_input(lender, data, name)
    db.add(lender)
    db.flush()
    audit(db, lender, "lender_created", actor)
    return lender


def update_lender(db: Session, lender_id: int, data: dict[str, Any], actor: User) -> Lender:
    """Edits a lender. A new name is shown on the cash statements of its loans once the nightly
    sync runs (see app.scripts.sync_cash_statements)."""
    # Same lock as loan writes, so a concurrent loan write never races this rename.
    lock_balances(db)
    lender = db.scalar(select(Lender).where(Lender.id == lender_id).with_for_update())
    if lender is None:
        raise not_found("Lender")
    name = clean_name(data["name"])
    assert_unique_name(db, name, exclude_id=lender.id)
    before = model_snapshot(lender)
    apply_input(lender, data, name)
    audit(db, lender, "lender_updated", actor, before=before)
    return lender


# --- Active loans per lender --------------------------------------------------------------------


@dataclass
class LenderExposure:
    """The active loans of one lender and their totals."""

    loans: list[tuple[Loan, LoanSummary]] = field(default_factory=list)

    def total(self, pick: Any) -> Decimal:
        return sum((Decimal(pick(loan, summary)) for loan, summary in self.loans), ZERO)

    @property
    def principal_amount(self) -> Decimal:
        return self.total(lambda loan, _: loan.principal_amount)

    @property
    def total_interest(self) -> Decimal:
        return self.total(lambda _, summary: summary.total_interest)

    @property
    def total_payable(self) -> Decimal:
        """Full principal + interest."""
        return self.principal_amount + self.total_interest

    @property
    def total_paid(self) -> Decimal:
        return self.total(lambda _, summary: summary.principal_paid + summary.interest_paid)

    @property
    def principal_outstanding(self) -> Decimal:
        return self.total(lambda _, summary: summary.principal_outstanding)

    @property
    def interest_outstanding(self) -> Decimal:
        return self.total(lambda _, summary: summary.interest_outstanding)

    @property
    def total_outstanding(self) -> Decimal:
        """What still has to be repaid."""
        return self.principal_outstanding + self.interest_outstanding

    @property
    def overdue_amount(self) -> Decimal:
        return self.total(lambda _, summary: summary.overdue_amount)

    @property
    def next_due_date(self) -> date | None:
        dates = [summary.next_installment.due_date for _, summary in self.loans if summary.next_installment]
        return min(dates, default=None)


def lender_exposures(db: Session, lender_ids: Sequence[int], as_of: date | None = None) -> dict[int, LenderExposure]:
    """Active loans of the given lenders (oldest first), in one query."""
    as_of = as_of or today_local()
    exposures: dict[int, LenderExposure] = defaultdict(LenderExposure)
    if not lender_ids:
        return exposures
    loans = db.scalars(
        loan_query().where(Loan.lender_id.in_(lender_ids), Loan.status == ACTIVE).order_by(Loan.start_date, Loan.id)
    ).all()
    for loan in loans:
        exposures[loan.lender_id].loans.append((loan, summarize(loan, as_of)))
    return exposures
