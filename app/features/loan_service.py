"""Loan management: the loans taken by the business, their schedule and their repayments.

Loan types (:class:`LoanTerms`)
    ``SHORT_TERM``
        A single installment: the whole principal plus a flat ``interest_amount`` (an IDR amount,
        0 allowed, not a rate: when a lender quotes a rate the user enters the resulting amount),
        due on one freely chosen date (``due_date``, any day from the start date, e.g. one week).
    ``LONG_TERM``
        ``term_months`` monthly installments from ``first_due_date`` (same day each month, or the
        last day of shorter months), with interest from ``annual_interest_rate`` (percent per year,
        fixed) and ``interest_method`` (:func:`build_schedule`):

        - ``FLAT`` (bunga flat): every month pays principal / term plus interest on the *original*
          principal, principal x rate / 12.
        - ``ANNUITY`` (bunga anuitas/efektif): every month pays the same installment; the interest
          is charged on the outstanding balance, so its share decreases and the principal share
          grows. The last installment absorbs the rounding.

    Amounts are whole IDR and the principal parts always add up to the principal exactly.

    Every field can be edited, including the type. New terms regenerate the schedule and existing
    repayments are re-allocated to it; an edit that would make the loan total (principal +
    interest) smaller than what has already been repaid is rejected.

Repayments (:func:`allocate`)
    Money paid back, out of the main cash account. A repayment is applied to the oldest installment
    first and, within an installment, to its interest before its principal; what is left goes to
    the next installment, so one repayment can cover a part of an installment or several of them.
    The allocation is re-derived from all non-voided repayments (in ``paid_date`` order) after every
    repayment or void, so a backdated repayment or a void always leaves every split consistent.
    A repayment cannot exceed what is still owed; a loan whose installments are all paid is
    ``PAID_OFF`` (and ``ACTIVE`` again if a repayment is voided).

Effects of a repayment, in the same database transaction:

- main cash account: a **debit** of its amount, queued like top-ups/transfers
  (reference ``loan_repayments:<id>:loan_repaid``); a void queues the offsetting credit
  (``loan_repayment_voided``). The daily audit reconciles repayments like the other sources;
- cash statements: one ``LOAN_REPAYMENT`` debit row for its full amount under the reference
  ``loans:<loan id>`` (see ``cash_statement_service``).

The principal/interest split of the money repaid is kept on both sides of the allocation: per
repayment (``loan_repayments.principal_amount`` / ``interest_amount``) and per installment
(``loan_installments.principal_paid`` / ``interest_paid``); both always add up to the same totals.

Every loan belongs to a lender (``lenders``); a new loan, or a loan moved to another lender, needs
an active one.
"""

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from app.core.decimal import money
from app.core.errors import conflict, not_found, validation
from app.db.models import Lender, Loan, LoanInstallment, LoanRepayment, User
from app.features.common import audit, model_snapshot, now_utc, today_local
from app.features.locks import lock_balances
from app.features.main_cash_account_service import LOAN_REPAYMENT_SOURCE, enqueue, repayment_effect

SHORT_TERM, LONG_TERM = "SHORT_TERM", "LONG_TERM"
LOAN_TYPES = (SHORT_TERM, LONG_TERM)
FLAT, ANNUITY = "FLAT", "ANNUITY"
INTEREST_METHODS = (FLAT, ANNUITY)
ACTIVE, PAID_OFF, CANCELLED = "ACTIVE", "PAID_OFF", "CANCELLED"
UNPAID, PARTIALLY_PAID, PAID = "UNPAID", "PARTIALLY_PAID", "PAID"

ZERO = Decimal("0")


# --- Schedule -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduledInstallment:
    sequence: int
    due_date: date
    principal: Decimal
    interest: Decimal

    @property
    def total(self) -> Decimal:
        return self.principal + self.interest


def add_months(day: date, months: int) -> date:
    """Same day ``months`` later, or the last day of that month when it is shorter."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def split_evenly(total: Decimal, parts: int) -> list[Decimal]:
    """Whole IDR parts adding up to ``total`` exactly; the last part takes the remainder."""
    base = (total / parts).to_integral_value(rounding=ROUND_DOWN)
    return [base] * (parts - 1) + [total - base * (parts - 1)]


def build_schedule(
    principal: Decimal, annual_rate: Decimal, term_months: int, method: str, first_due_date: date
) -> list[ScheduledInstallment]:
    """The installments of a loan (see the module docstring for the rules)."""
    if method not in INTEREST_METHODS:
        raise ValueError(f"unknown interest method {method!r}")
    if term_months < 1:
        raise ValueError("term_months must be at least 1")
    principal = money(principal)
    monthly_rate = Decimal(annual_rate) / Decimal(1200)
    due_dates = [add_months(first_due_date, offset) for offset in range(term_months)]
    if method == FLAT or monthly_rate == 0:
        principals = split_evenly(principal, term_months)
        interests = split_evenly(money(principal * monthly_rate * term_months), term_months)
    else:
        with localcontext() as context:
            context.prec = 50
            growth = (1 + monthly_rate) ** term_months
            installment = money(principal * monthly_rate * growth / (growth - 1))
        principals, interests, balance = [], [], principal
        for sequence in range(1, term_months + 1):
            interest = money(balance * monthly_rate)
            part = balance if sequence == term_months else min(max(installment - interest, ZERO), balance)
            principals.append(part)
            interests.append(interest)
            balance -= part
    return [
        ScheduledInstallment(sequence, due, principal_part, interest_part)
        for sequence, (due, principal_part, interest_part) in enumerate(
            zip(due_dates, principals, interests, strict=True), start=1
        )
    ]


@dataclass(frozen=True)
class LoanTerms:
    """Everything that shapes a loan's schedule. Two loans with equal terms have the same schedule,
    so an edit regenerates the schedule exactly when its terms differ."""

    loan_type: str
    principal: Decimal
    first_due_date: date
    # SHORT_TERM
    interest_amount: Decimal | None = None
    # LONG_TERM
    annual_rate: Decimal | None = None
    method: str | None = None
    term_months: int | None = None

    @classmethod
    def from_input(cls, data: dict[str, Any]) -> LoanTerms:
        """From the API input: ``due_date``/``interest_amount`` for a short-term loan,
        ``first_due_date``/``annual_interest_rate``/``interest_method``/``term_months`` for a
        long-term one."""
        principal = money(data["principal_amount"])
        if data["loan_type"] == SHORT_TERM:
            return cls(SHORT_TERM, principal, data["due_date"], interest_amount=money(data["interest_amount"]))
        if data["loan_type"] != LONG_TERM:
            raise ValueError(f"unknown loan type {data['loan_type']!r}")
        return cls(
            LONG_TERM,
            principal,
            data["first_due_date"],
            annual_rate=Decimal(data["annual_interest_rate"]),
            method=data["interest_method"],
            term_months=data["term_months"],
        )

    @classmethod
    def of(cls, loan: Loan) -> LoanTerms:
        return cls(
            loan.loan_type,
            Decimal(loan.principal_amount),
            loan.first_due_date,
            interest_amount=None if loan.interest_amount is None else Decimal(loan.interest_amount),
            annual_rate=None if loan.annual_interest_rate is None else Decimal(loan.annual_interest_rate),
            method=loan.interest_method,
            term_months=loan.term_months,
        )

    @property
    def due_date_field(self) -> str:
        """Name of the due date in the API input, for error messages."""
        return "due_date" if self.loan_type == SHORT_TERM else "first_due_date"

    def schedule(self) -> list[ScheduledInstallment]:
        if self.loan_type == SHORT_TERM:
            assert self.interest_amount is not None
            return [ScheduledInstallment(1, self.first_due_date, self.principal, self.interest_amount)]
        assert self.annual_rate is not None and self.method is not None and self.term_months is not None
        return build_schedule(self.principal, self.annual_rate, self.term_months, self.method, self.first_due_date)

    def apply(self, loan: Loan) -> None:
        """Stores the terms on the loan (clearing the fields of the other type)."""
        loan.loan_type, loan.principal_amount, loan.first_due_date = (
            self.loan_type,
            self.principal,
            self.first_due_date,
        )
        loan.interest_amount = self.interest_amount
        loan.annual_interest_rate, loan.interest_method, loan.term_months = (
            self.annual_rate,
            self.method,
            self.term_months,
        )


# --- Allocation ---------------------------------------------------------------------------------


class OverpaymentError(ValueError):
    """A repayment is larger than what is still owed."""


@dataclass(frozen=True)
class RepaymentSplit:
    principal: Decimal
    interest: Decimal
    first_installment: int | None
    last_installment: int | None


@dataclass(frozen=True)
class InstallmentPaid:
    principal_paid: Decimal
    interest_paid: Decimal
    # Index (in the given order) of the repayment that settled the installment, if settled.
    settled_by: int | None


def allocate(
    dues: Sequence[tuple[Decimal, Decimal]], amounts: Sequence[Decimal]
) -> tuple[list[InstallmentPaid], list[RepaymentSplit]]:
    """Applies repayments (already in payment order) to installments given as ``(principal,
    interest)`` in schedule order: oldest installment first, interest before principal.

    Returns what is paid on every installment and how every repayment was split. Raises
    :class:`OverpaymentError` if the repayments exceed the total owed.
    """
    principal_paid = [ZERO] * len(dues)
    interest_paid = [ZERO] * len(dues)
    settled_by: list[int | None] = [None] * len(dues)
    splits: list[RepaymentSplit] = []
    for index, amount in enumerate(amounts):
        remaining, to_principal, to_interest = Decimal(amount), ZERO, ZERO
        first: int | None = None
        last: int | None = None
        for position, (principal, interest) in enumerate(dues):
            if remaining == 0:
                break
            interest_part = min(remaining, interest - interest_paid[position])
            remaining -= interest_part
            principal_part = min(remaining, principal - principal_paid[position])
            remaining -= principal_part
            if interest_part or principal_part:
                interest_paid[position] += interest_part
                principal_paid[position] += principal_part
                to_interest += interest_part
                to_principal += principal_part
                first = position + 1 if first is None else first
                last = position + 1
                if principal_paid[position] == principal and interest_paid[position] == interest:
                    settled_by[position] = index
        if remaining > 0:
            raise OverpaymentError(f"repayment exceeds the outstanding amount by {remaining}")
        splits.append(RepaymentSplit(to_principal, to_interest, first, last))
    paid = [
        InstallmentPaid(principal_paid[position], interest_paid[position], settled_by[position])
        for position in range(len(dues))
    ]
    return paid, splits


def active_repayments(loan: Loan) -> list[LoanRepayment]:
    """Non-voided repayments in payment order (date, then entry order)."""
    return sorted(
        (item for item in loan.repayments if item.voided_at is None),
        # A repayment being created has no id yet: it was entered last.
        key=lambda item: (item.paid_date, item.id is None, item.id or 0),
    )


def reallocate(loan: Loan) -> None:
    """Re-derives the paid amounts of every installment, the split and installments of every
    non-voided repayment, and the loan status, from the non-voided repayments."""
    repayments = active_repayments(loan)
    paid, splits = allocate(
        [(Decimal(item.principal_amount), Decimal(item.interest_amount)) for item in loan.installments],
        [Decimal(item.amount) for item in repayments],
    )
    for repayment, split in zip(repayments, splits, strict=True):
        repayment.principal_amount, repayment.interest_amount = split.principal, split.interest
        repayment.first_installment, repayment.last_installment = split.first_installment, split.last_installment
    for installment, state in zip(loan.installments, paid, strict=True):
        installment.principal_paid, installment.interest_paid = state.principal_paid, state.interest_paid
        total_paid = state.principal_paid + state.interest_paid
        if state.settled_by is not None:
            installment.status, installment.paid_date = PAID, repayments[state.settled_by].paid_date
        else:
            installment.status, installment.paid_date = (PARTIALLY_PAID if total_paid else UNPAID), None
    if loan.status != CANCELLED:
        loan.status = PAID_OFF if all(item.status == PAID for item in loan.installments) else ACTIVE


# --- Reading ------------------------------------------------------------------------------------


def installment_outstanding(installment: LoanInstallment) -> Decimal:
    return (
        Decimal(installment.principal_amount)
        + Decimal(installment.interest_amount)
        - Decimal(installment.principal_paid)
        - Decimal(installment.interest_paid)
    )


@dataclass(frozen=True)
class LoanSummary:
    total_interest: Decimal
    principal_paid: Decimal
    interest_paid: Decimal
    principal_outstanding: Decimal
    interest_outstanding: Decimal
    installments_total: int
    installments_paid: int
    next_installment: LoanInstallment | None
    overdue_installments: int
    overdue_amount: Decimal
    maturity_date: date | None

    @property
    def total_outstanding(self) -> Decimal:
        return self.principal_outstanding + self.interest_outstanding


def is_overdue(installment: LoanInstallment, as_of: date) -> bool:
    return installment.status != PAID and installment.due_date < as_of


def summarize(loan: Loan, as_of: date | None = None) -> LoanSummary:
    as_of = as_of or today_local()
    installments = list(loan.installments)
    principal_paid = sum((Decimal(item.principal_paid) for item in installments), ZERO)
    interest_paid = sum((Decimal(item.interest_paid) for item in installments), ZERO)
    total_interest = sum((Decimal(item.interest_amount) for item in installments), ZERO)
    unpaid = [item for item in installments if item.status != PAID]
    overdue = [item for item in unpaid if is_overdue(item, as_of)] if loan.status != CANCELLED else []
    return LoanSummary(
        total_interest=total_interest,
        principal_paid=principal_paid,
        interest_paid=interest_paid,
        principal_outstanding=Decimal(loan.principal_amount) - principal_paid if loan.status != CANCELLED else ZERO,
        interest_outstanding=total_interest - interest_paid if loan.status != CANCELLED else ZERO,
        installments_total=len(installments),
        installments_paid=len(installments) - len(unpaid),
        next_installment=unpaid[0] if unpaid and loan.status != CANCELLED else None,
        overdue_installments=len(overdue),
        overdue_amount=sum((installment_outstanding(item) for item in overdue), ZERO),
        maturity_date=installments[-1].due_date if installments else None,
    )


def loan_query() -> Select[tuple[Loan]]:
    return select(Loan).options(
        selectinload(Loan.installments), selectinload(Loan.repayments), selectinload(Loan.lender)
    )


def get_loan(db: Session, loan_id: int, *, lock: bool = False) -> Loan:
    stmt = loan_query().where(Loan.id == loan_id)
    if lock:
        stmt = stmt.with_for_update(of=Loan).execution_options(populate_existing=True)
    loan = db.scalar(stmt)
    if loan is None:
        raise not_found("Loan")
    return loan


# --- Writes -------------------------------------------------------------------------------------


def validate_lender(db: Session, lender_id: int, current: int | None = None) -> None:
    """The lender must exist, and be active unless the loan already belongs to it."""
    lender = db.get(Lender, lender_id)
    if lender is None:
        raise validation("lender_id", "Lender does not exist.")
    if not lender.is_active and lender_id != current:
        raise validation("lender_id", "Lender is inactive; reactivate it to record a new loan.")


def validate_input(data: dict[str, Any], terms: LoanTerms) -> None:
    if terms.first_due_date < data["start_date"]:
        raise validation(terms.due_date_field, "The loan cannot be due before it starts.")
    if terms.principal <= 0:
        raise validation("principal_amount", "Principal must be at least 1 IDR.")


def replace_schedule(db: Session, loan: Loan, terms: LoanTerms) -> None:
    if loan.installments:
        # Delete the old rows first: the unit of work would insert the new installment 1 before
        # deleting the old one, breaking the unique (loan_id, sequence).
        loan.installments.clear()
        db.flush()
    for item in terms.schedule():
        loan.installments.append(
            LoanInstallment(
                sequence=item.sequence,
                due_date=item.due_date,
                principal_amount=item.principal,
                interest_amount=item.interest,
                principal_paid=ZERO,
                interest_paid=ZERO,
                status=UNPAID,
            )
        )


def apply_input(loan: Loan, data: dict[str, Any], terms: LoanTerms) -> None:
    loan.lender_id = data["lender_id"]
    loan.reference_number = data.get("reference_number")
    loan.start_date = data["start_date"]
    loan.notes = data.get("notes")
    terms.apply(loan)


def create_loan(db: Session, data: dict[str, Any], actor: User) -> Loan:
    lock_balances(db)
    terms = LoanTerms.from_input(data)
    validate_input(data, terms)
    validate_lender(db, data["lender_id"])
    loan = Loan(created_by=actor.id, status=ACTIVE)
    apply_input(loan, data, terms)
    replace_schedule(db, loan, terms)
    db.add(loan)
    db.flush()
    audit(db, loan, "loan_created", actor)
    return loan


def update_loan(db: Session, loan_id: int, data: dict[str, Any], actor: User) -> Loan:
    """Edits a loan, including its type and terms.

    New terms regenerate the schedule and the existing repayments are re-allocated to it (oldest
    installment first, interest before principal), so the paid/partially paid installments and the
    loan status follow the edit. The edit is rejected when the loan would become smaller than what
    has already been repaid: the new total payable (principal + interest of the new schedule) must
    cover every non-voided repayment.
    """
    lock_balances(db)
    loan = get_loan(db, loan_id, lock=True)
    if loan.status == CANCELLED:
        raise conflict("LOAN_CANCELLED", "A cancelled loan cannot be edited.")
    terms = LoanTerms.from_input(data)
    validate_input(data, terms)
    validate_lender(db, data["lender_id"], current=loan.lender_id)
    repayments = active_repayments(loan)
    terms_changed = terms != LoanTerms.of(loan)
    if terms_changed and repayments:
        schedule = terms.schedule()
        principal = sum((item.principal for item in schedule), ZERO)
        interest = sum((item.interest for item in schedule), ZERO)
        repaid = sum((Decimal(item.amount) for item in repayments), ZERO)
        if principal + interest < repaid:
            raise validation(
                "principal_amount",
                f"The edited loan would total {principal + interest:,} (principal {principal:,} + interest "
                f"{interest:,}), less than the {repaid:,} already repaid. Increase the loan amount or void "
                "repayments first.",
            )
    first_paid = min((item.paid_date for item in repayments), default=None)
    if first_paid is not None and data["start_date"] > first_paid:
        raise validation("start_date", "The loan cannot start after its first repayment.")
    before = model_snapshot(loan)
    apply_input(loan, data, terms)
    # The loaded lender would be stale after a change of lender_id.
    db.expire(loan, ["lender"])
    if terms_changed:
        replace_schedule(db, loan, terms)
        reallocate(loan)
    audit(db, loan, "loan_updated", actor, before=before)
    # Lender, contract number, start date and the installments covered appear on the cash
    # statements; the nightly sync picks the change up (see app.scripts.sync_cash_statements).
    return loan


def repay_loan(
    db: Session, loan_id: int, actor: User, paid_date: date, amount: Decimal | None, notes: str | None
) -> LoanRepayment:
    """Records a repayment (debited from the main cash account). Without ``amount`` it pays what is
    still due on the next unpaid installment."""
    lock_balances(db)
    loan = get_loan(db, loan_id, lock=True)
    if loan.status == CANCELLED:
        raise conflict("LOAN_CANCELLED", "A cancelled loan cannot be repaid.")
    if loan.status == PAID_OFF:
        raise conflict("LOAN_PAID_OFF", "This loan is already paid off.")
    if paid_date > today_local():
        raise validation("paid_date", "Repayment date cannot be in the future.")
    if paid_date < loan.start_date:
        raise validation("paid_date", "Repayment date cannot be before the loan starts.")
    summary = summarize(loan)
    if amount is None:
        assert summary.next_installment is not None
        value = installment_outstanding(summary.next_installment)
    else:
        value = money(amount)
    if value <= 0:
        raise validation("amount", "Repayment must be at least 1 IDR.")
    if value > summary.total_outstanding:
        raise validation("amount", f"Repayment cannot exceed the outstanding amount of {summary.total_outstanding}.")
    before = model_snapshot(loan)
    repayment = LoanRepayment(
        loan_id=loan.id,
        paid_date=paid_date,
        amount=value,
        # Provisional split, replaced by reallocate() below before anything is written.
        principal_amount=ZERO,
        interest_amount=value,
        notes=notes,
        created_by=actor.id,
    )
    loan.repayments.append(repayment)
    reallocate(loan)
    db.flush()
    audit(
        db,
        loan,
        "loan_repaid",
        actor,
        before=before,
        after=model_snapshot(loan) | {"repayment": model_snapshot(repayment)},
    )
    enqueue(db, LOAN_REPAYMENT_SOURCE, repayment.id, "loan_repaid", repayment_effect(False, value))
    return repayment


def void_repayment(db: Session, loan_id: int, repayment_id: int, actor: User, reason: str) -> LoanRepayment:
    """Voids a repayment: the main cash account is credited back and the remaining repayments are
    re-allocated. Voiding twice does nothing."""
    lock_balances(db)
    loan = get_loan(db, loan_id, lock=True)
    repayment = next((item for item in loan.repayments if item.id == repayment_id), None)
    if repayment is None:
        raise not_found("Loan repayment")
    if repayment.voided_at is not None:
        return repayment
    before = model_snapshot(loan)
    repayment.voided_at, repayment.voided_by, repayment.void_reason = now_utc(), actor.id, reason
    reallocate(loan)
    db.flush()
    audit(
        db,
        loan,
        "loan_repayment_voided",
        actor,
        before=before,
        reason=reason,
        after=model_snapshot(loan) | {"repayment": model_snapshot(repayment)},
    )
    amount = Decimal(repayment.amount)
    delta = repayment_effect(True, amount) - repayment_effect(False, amount)
    enqueue(db, LOAN_REPAYMENT_SOURCE, repayment.id, "loan_repayment_voided", delta)
    return repayment


def cancel_loan(db: Session, loan_id: int, actor: User, reason: str) -> Loan:
    """Cancels a loan entered by mistake. Only possible while it has no (non-voided) repayment."""
    lock_balances(db)
    loan = get_loan(db, loan_id, lock=True)
    if loan.status == CANCELLED:
        return loan
    if active_repayments(loan):
        raise conflict("LOAN_HAS_REPAYMENTS", "A loan with repayments cannot be cancelled; void them first.")
    before = model_snapshot(loan)
    loan.status, loan.cancelled_at, loan.cancelled_by, loan.cancel_reason = CANCELLED, now_utc(), actor.id, reason
    audit(db, loan, "loan_cancelled", actor, before=before, reason=reason)
    return loan
