"""Checks for loan management (app/features/loan_service.py).

Rules under test:

- the schedule: FLAT and ANNUITY installments in whole IDR whose principal adds up exactly, due
  monthly from the first due date (clamped to the end of shorter months);
- repayments are applied oldest installment first, interest before principal, re-derived after
  every repayment or void, and cannot exceed what is owed;
- a repayment is a debit of the main cash account (queued, reference
  ``loan_repayments:<id>:loan_repaid``) and a void credits it back; the daily audit reconciles them;
- a repayment appears in cash_statements as a LOAN_PRINCIPAL and a LOAN_INTEREST debit under the
  reference ``loans:<id>``;
- SHORT_TERM: one installment on a chosen date with a flat interest amount; LONG_TERM: monthly
  installments from a rate; every field is editable, rejected below the amount already repaid.

Like the other finance tests these run against the real PostgreSQL test database (see
tests/conftest.py), which is wiped before and after every test.
"""

import os
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import CashStatement, Lender, Loan, LoanInstallment, MainCashAccountLog, User
from app.features.cash_statement_service import plan_sync, rebuild_cash_statements
from app.features.common import today_local
from app.features.loan_service import (
    LoanTerms,
    OverpaymentError,
    add_months,
    allocate,
    build_schedule,
    cancel_loan,
    create_loan,
    get_loan,
    installment_outstanding,
    repay_loan,
    summarize,
    update_loan,
    void_repayment,
)
from app.features.main_cash_account_audit import audit_main_cash_account
from app.features.main_cash_account_events import drain
from app.features.main_cash_account_service import adjust, get_account, log_total, parse_reference
from app.main import app

# Lenders created for every test by the autouse fixture below: name -> id.
LENDER: dict[str, int] = {}

TODAY = today_local()
START = TODAY - timedelta(days=120)
D = Decimal


@pytest.fixture(autouse=True)
def lenders(db: Session) -> Iterator[None]:
    for name in ("Bank BRI", "Bank BRI Syariah", "Bank Mandiri", "Koperasi Maju"):
        lender = Lender(name=name, is_active=True)
        db.add(lender)
        db.flush()
        LENDER[name] = lender.id
    db.commit()
    yield
    LENDER.clear()


# --- Pure rules: schedule -----------------------------------------------------------------------


def test_flat_schedule() -> None:
    schedule = build_schedule(D("12000000"), D("12"), 12, "FLAT", date(2026, 1, 15))

    assert {(item.principal, item.interest) for item in schedule} == {(D("1000000"), D("120000"))}
    assert [item.sequence for item in schedule] == list(range(1, 13))
    assert schedule[-1].due_date == date(2026, 12, 15)


def test_flat_schedule_puts_the_rounding_on_the_last_installment() -> None:
    schedule = build_schedule(D("1000000"), D("10"), 3, "FLAT", date(2026, 1, 15))

    assert [(item.principal, item.interest) for item in schedule] == [
        (D("333333"), D("8333")),
        (D("333333"), D("8333")),
        (D("333334"), D("8334")),
    ]
    assert sum(item.principal for item in schedule) == D("1000000")
    assert sum(item.interest for item in schedule) == D("25000")  # 1,000,000 x 10 % / 12 x 3


def test_annuity_schedule() -> None:
    schedule = build_schedule(D("12000000"), D("12"), 12, "ANNUITY", date(2026, 1, 15))

    # Standard annuity: 12,000,000 x 1 % / (1 - 1.01^-12) = 1,066,185.46 per month.
    assert {item.total for item in schedule[:-1]} == {D("1066185")}
    assert schedule[-1].total == D("1066191"), "the last installment absorbs the rounding"
    assert (schedule[0].principal, schedule[0].interest) == (D("946185"), D("120000"))
    interests = [item.interest for item in schedule]
    assert interests == sorted(interests, reverse=True), "interest is charged on the declining balance"
    assert sum(item.principal for item in schedule) == D("12000000")
    assert sum(item.interest for item in schedule) == D("794226")


@pytest.mark.parametrize("method", ["FLAT", "ANNUITY"])
def test_zero_interest(method: str) -> None:
    schedule = build_schedule(D("900000"), D("0"), 3, method, date(2026, 1, 15))

    assert [(item.principal, item.interest) for item in schedule] == [(D("300000"), D("0"))] * 3


def test_due_dates_follow_the_first_due_date_day() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 1, 31), 2) == date(2026, 3, 31), "back to the 31st after February"
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
    schedule = build_schedule(D("300"), D("0"), 3, "FLAT", date(2026, 1, 31))
    assert [item.due_date for item in schedule] == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]


# --- Pure rules: allocation ---------------------------------------------------------------------


def test_allocation_interest_first_oldest_first() -> None:
    dues = [(D("1000"), D("100")), (D("1000"), D("100")), (D("1000"), D("100"))]

    paid, splits = allocate(dues, [D("50"), D("1150"), D("1200")])

    # 50: interest of #1 only; 1,150: rest of #1 (50 interest + 1,000 principal) then 100 interest
    # of #2; 1,200: principal of #2 then interest and 100 principal of #3.
    assert [(item.principal, item.interest, item.first_installment, item.last_installment) for item in splits] == [
        (D("0"), D("50"), 1, 1),
        (D("1000"), D("150"), 1, 2),
        (D("1100"), D("100"), 2, 3),
    ]
    assert [(item.principal_paid, item.interest_paid, item.settled_by) for item in paid] == [
        (D("1000"), D("100"), 1),
        (D("1000"), D("100"), 2),
        (D("100"), D("100"), None),
    ]


def test_allocation_rejects_overpayment() -> None:
    with pytest.raises(OverpaymentError):
        allocate([(D("1000"), D("100"))], [D("1000"), D("101")])


# --- Helpers ------------------------------------------------------------------------------------


def loan_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "lender_id": LENDER["Bank BRI"],
        "reference_number": "KUR-001",
        "loan_type": "LONG_TERM",
        "principal_amount": D("12000000"),
        "annual_interest_rate": D("12"),
        "interest_method": "FLAT",
        "term_months": 12,
        "start_date": START,
        "first_due_date": START + timedelta(days=30),
        "notes": None,
    }
    return data | overrides


def new_loan(db: Session, actor: User, **overrides: object) -> Loan:
    loan = create_loan(db, loan_data(**overrides), actor)
    db.commit()
    return loan


def repay(db: Session, actor: User, loan: Loan, amount: str | None = None, day: date = TODAY) -> int:
    repayment = repay_loan(db, loan.id, actor, day, None if amount is None else D(amount), None)
    db.commit()
    return repayment.id


def reload(db: Session, loan: Loan) -> Loan:
    db.expire_all()
    return get_loan(db, loan.id)


def main_amount(db: Session) -> Decimal | None:
    drain()
    db.expire_all()
    account = get_account(db)
    return None if account is None else D(account.current_amount)


def statement_rows(db: Session) -> list[tuple[str, str, Decimal, str, str]]:
    """(date, category, amount, description, reference) in payment order.

    ``cash_statements`` is no longer kept current by the write itself (see
    app.features.cash_statement_service); it is refreshed once a night instead
    (app.scripts.sync_cash_statements). Tests stand in for that nightly job with the full rebuild
    before reading the table, so the month of the test data never matters.
    """
    rebuild_cash_statements(db)
    db.expire_all()
    rows = db.scalars(
        select(CashStatement).order_by(
            CashStatement.statement_date, CashStatement.payment_sequence, CashStatement.category.desc()
        )
    ).all()
    return [
        (row.statement_date.isoformat(), row.category, D(row.amount), row.description, row.reference) for row in rows
    ]


# --- Loans and schedules ------------------------------------------------------------------------


def test_create_loan_builds_its_schedule(db: Session, actor: User) -> None:
    loan = reload(db, new_loan(db, actor))

    assert (loan.status, loan.loan_type, len(loan.installments)) == ("ACTIVE", "LONG_TERM", 12)
    assert {item.status for item in loan.installments} == {"UNPAID"}
    summary = summarize(loan)
    assert (summary.principal_outstanding, summary.interest_outstanding) == (D("12000000"), D("1440000"))
    assert summary.next_installment is not None and summary.next_installment.sequence == 1
    # Due 30, 60, 90 days after START (about); START is 120 days ago, so some are already overdue.
    assert summary.overdue_installments == sum(1 for item in loan.installments if item.due_date < TODAY)


def short_loan_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "lender_id": LENDER["Koperasi Maju"],
        "reference_number": None,
        "loan_type": "SHORT_TERM",
        "principal_amount": D("5000000"),
        "interest_amount": D("150000"),
        "start_date": START,
        "due_date": START + timedelta(days=7),
        "notes": None,
    }
    return data | overrides


def test_short_term_schedule_is_one_installment_with_a_flat_interest_amount() -> None:
    terms = LoanTerms.from_input(short_loan_data())

    assert [(item.sequence, item.due_date, item.principal, item.interest) for item in terms.schedule()] == [
        (1, START + timedelta(days=7), D("5000000"), D("150000"))
    ]
    assert (terms.annual_rate, terms.method, terms.term_months) == (None, None, None)


def test_short_term_loan(db: Session, actor: User) -> None:
    loan = create_loan(db, short_loan_data(), actor)
    db.commit()
    loan = reload(db, loan)

    assert (loan.loan_type, loan.interest_amount, loan.first_due_date) == (
        "SHORT_TERM",
        D("150000"),
        START + timedelta(days=7),
    )
    assert (loan.annual_interest_rate, loan.interest_method, loan.term_months) == (None, None, None)
    (installment,) = loan.installments
    assert (installment.principal_amount, installment.interest_amount, installment.due_date) == (
        D("5000000"),
        D("150000"),
        START + timedelta(days=7),
    )
    summary = summarize(loan)
    assert (summary.total_interest, summary.total_outstanding, summary.maturity_date) == (
        D("150000"),
        D("5150000"),
        START + timedelta(days=7),
    )


def test_short_term_interest_can_be_zero_and_due_on_the_start_date(db: Session, actor: User) -> None:
    loan = create_loan(db, short_loan_data(interest_amount=D("0"), due_date=START), actor)
    db.commit()
    loan = reload(db, loan)

    assert [(item.principal_amount, item.interest_amount, item.due_date) for item in loan.installments] == [
        (D("5000000"), D("0"), START)
    ]


def test_short_term_repayments(db: Session, actor: User) -> None:
    loan = create_loan(db, short_loan_data(), actor)
    db.commit()

    repay(db, actor, loan, "2000000", day=TODAY - timedelta(days=1))
    loan = reload(db, loan)
    (installment,) = loan.installments
    assert (installment.status, installment.interest_paid, installment.principal_paid) == (
        "PARTIALLY_PAID",
        D("150000"),
        D("1850000"),
    )
    assert loan.status == "ACTIVE"

    repay(db, actor, loan)  # the rest: 3,150,000
    loan = reload(db, loan)
    assert (loan.repayments[1].amount, loan.installments[0].status, loan.status) == (D("3150000"), "PAID", "PAID_OFF")
    # A single installment has nothing to tell apart in the statement.
    assert [(row[2], row[3]) for row in statement_rows(db)] == [
        (D("2000000"), "Loan repayment: Koperasi Maju"),
        (D("3150000"), "Loan repayment: Koperasi Maju"),
    ]
    assert main_amount(db) == D("-5150000")


def test_short_term_edit_below_the_repaid_amount_is_rejected(db: Session, actor: User) -> None:
    loan = create_loan(db, short_loan_data(), actor)
    db.commit()
    repay(db, actor, loan, "5100000")

    with pytest.raises(AppError) as error:
        update_loan(db, loan.id, short_loan_data(interest_amount=D("0")), actor)
    db.rollback()
    assert error.value.errors["principal_amount"][0].startswith(
        "The edited loan would total 5,000,000 (principal 5,000,000 + interest 0), less than the 5,100,000"
    )

    update_loan(db, loan.id, short_loan_data(interest_amount=D("100000")), actor)
    db.commit()
    assert reload(db, loan).status == "PAID_OFF", "5,000,000 + 100,000 is exactly what was repaid"


def test_the_type_can_be_changed_and_the_repayments_follow(db: Session, actor: User) -> None:
    loan = create_loan(db, short_loan_data(), actor)
    db.commit()
    repay(db, actor, loan, "1000000")

    # Turned into 5 monthly installments of 1,000,000 + 50,000 (12 % flat).
    update_loan(
        db,
        loan.id,
        loan_data(
            principal_amount=D("5000000"), term_months=5, lender_id=LENDER["Koperasi Maju"], reference_number=None
        ),
        actor,
    )
    db.commit()

    loan = reload(db, loan)
    assert (loan.loan_type, loan.interest_amount, loan.term_months, len(loan.installments)) == (
        "LONG_TERM",
        None,
        5,
        5,
    )
    assert [item.status for item in loan.installments[:2]] == ["PARTIALLY_PAID", "UNPAID"]
    assert statement_rows(db)[0][3] == "Loan repayment: Koperasi Maju (installment 1)"


def test_short_term_due_date_cannot_precede_the_start(db: Session, actor: User) -> None:
    with pytest.raises(AppError) as error:
        create_loan(db, short_loan_data(due_date=START - timedelta(days=1)), actor)
    assert "due_date" in error.value.errors


def test_first_due_date_cannot_precede_the_start(db: Session, actor: User) -> None:
    with pytest.raises(AppError) as error:
        create_loan(db, loan_data(first_due_date=START - timedelta(days=1)), actor)
    assert error.value.code == "VALIDATION_ERROR"


def test_terms_can_be_edited_after_repayments_and_the_repayments_follow(db: Session, actor: User) -> None:
    adjust(db, "credit", D("50000000"), "Opening", actor)
    db.commit()
    loan = new_loan(db, actor)  # 12,000,000 flat 12 %: 1,120,000 per installment
    repay(db, actor, loan, "1500000")
    before_edit = main_amount(db)

    # Halve the loan: 500,000 + 60,000 = 560,000 per installment.
    update_loan(db, loan.id, loan_data(principal_amount=D("6000000")), actor)
    db.commit()

    loan = reload(db, loan)
    assert {item.principal_amount + item.interest_amount for item in loan.installments} == {D("560000")}
    # The same 1,500,000 now pays installments 1 and 2 and 380,000 of installment 3.
    assert [item.status for item in loan.installments[:4]] == ["PAID", "PAID", "PARTIALLY_PAID", "UNPAID"]
    assert (loan.installments[2].interest_paid, loan.installments[2].principal_paid) == (D("60000"), D("320000"))
    (repayment,) = loan.repayments
    assert (repayment.first_installment, repayment.last_installment) == (1, 3)
    # The split is re-derived on both the repayment and the installments, with the same totals.
    assert (repayment.principal_amount, repayment.interest_amount) == (D("1320000"), D("180000"))
    assert sum(item.principal_paid for item in loan.installments) == D("1320000")
    assert sum(item.interest_paid for item in loan.installments) == D("180000")
    assert statement_rows(db)[0][3] == "Loan repayment: Bank BRI KUR-001 (installments 1-3)"
    assert main_amount(db) == before_edit, "editing a loan never moves money"

    # Moving the loan to another lender reaches the statement too.
    update_loan(db, loan.id, loan_data(principal_amount=D("6000000"), lender_id=LENDER["Bank BRI Syariah"]), actor)
    db.commit()
    assert statement_rows(db)[0][3] == "Loan repayment: Bank BRI Syariah KUR-001 (installments 1-3)"


def test_edit_below_the_repaid_amount_is_rejected(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    repay(db, actor, loan, "1500000")

    # 1,000,000 at 12 % flat over 1 month = 1,000,000 + 10,000 < 1,500,000 already repaid.
    with pytest.raises(AppError) as error:
        update_loan(db, loan.id, loan_data(principal_amount=D("1000000"), term_months=1), actor)
    db.rollback()

    assert error.value.status_code == 422
    assert error.value.errors == {
        "principal_amount": [
            "The edited loan would total 1,010,000 (principal 1,000,000 + interest 10,000), less than the "
            "1,500,000 already repaid. Increase the loan amount or void repayments first."
        ]
    }
    loan = reload(db, loan)
    assert (loan.principal_amount, len(loan.installments)) == (D("12000000"), 12), "nothing was changed"


def test_edit_to_exactly_the_repaid_amount_pays_the_loan_off(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    repay(db, actor, loan, "1500000")

    update_loan(
        db, loan.id, loan_data(principal_amount=D("1500000"), annual_interest_rate=D("0"), term_months=3), actor
    )
    db.commit()

    loan = reload(db, loan)
    assert loan.status == "PAID_OFF"
    assert {item.status for item in loan.installments} == {"PAID"}
    assert summarize(loan).total_outstanding == 0


# --- Repayments ---------------------------------------------------------------------------------


def test_repayment_without_amount_pays_the_next_installment(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    repayment_id = repay(db, actor, loan)

    loan = reload(db, loan)
    (repayment,) = loan.repayments
    assert (repayment.id, repayment.amount) == (repayment_id, D("1120000"))
    assert (repayment.first_installment, repayment.last_installment) == (1, 1)
    assert (loan.installments[0].principal_paid, loan.installments[0].interest_paid) == (D("1000000"), D("120000"))
    assert (loan.installments[0].status, loan.installments[0].paid_date) == ("PAID", TODAY)
    assert loan.installments[1].status == "UNPAID"


def test_manual_amount_fills_the_oldest_installment_and_the_rest_goes_to_the_next(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)  # 1,120,000 per installment (1,000,000 principal + 120,000 interest)

    repay(db, actor, loan, "1500000")

    loan = reload(db, loan)
    first, second, third = loan.installments[:3]
    assert (first.status, first.principal_paid, first.interest_paid, first.paid_date) == (
        "PAID",
        D("1000000"),
        D("120000"),
        TODAY,
    )
    # The remaining 380,000 goes to installment 2: its interest first, then principal.
    assert (second.status, second.interest_paid, second.principal_paid) == ("PARTIALLY_PAID", D("120000"), D("260000"))
    assert (second.paid_date, third.status) == (None, "UNPAID")
    summary = summarize(loan)
    assert summary.next_installment is not None
    assert (summary.next_installment.sequence, installment_outstanding(summary.next_installment)) == (2, D("740000"))

    # Without an amount, the next repayment settles what is left of installment 2.
    repay(db, actor, loan)
    loan = reload(db, loan)
    assert (loan.repayments[1].amount, loan.installments[1].status, loan.installments[2].status) == (
        D("740000"),
        "PAID",
        "UNPAID",
    )


def test_repayment_debits_the_main_cash_account(db: Session, actor: User) -> None:
    adjust(db, "credit", D("50000000"), "Opening", actor)
    db.commit()
    loan = new_loan(db, actor)

    repayment_id = repay(db, actor, loan)

    assert main_amount(db) == D("48880000")
    entry = db.scalars(select(MainCashAccountLog).order_by(MainCashAccountLog.id.desc())).first()
    assert (entry.direction, entry.amount, entry.reference) == (
        "debit",
        D("1120000"),
        f"loan_repayments:{repayment_id}:loan_repaid",
    )
    assert (entry.source_table, entry.source_id) == ("loan_repayments", repayment_id)
    assert parse_reference(entry.reference) == {"type": "loan_repayments", "id": repayment_id, "action": "loan_repaid"}
    assert log_total(db) == main_amount(db)


def test_repayment_is_one_cash_statement_row_for_its_full_amount(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    yesterday = TODAY - timedelta(days=1)
    first = repay(db, actor, loan, day=yesterday)
    second = repay(db, actor, loan, "1500000")  # rest goes to installment 2 and part of 3

    reference = f"loans:{loan.id}"
    assert statement_rows(db) == [
        (
            yesterday.isoformat(),
            "LOAN_REPAYMENT",
            D("1120000"),
            "Loan repayment: Bank BRI KUR-001 (installment 1)",
            reference,
        ),
        (
            TODAY.isoformat(),
            "LOAN_REPAYMENT",
            D("1500000"),
            "Loan repayment: Bank BRI KUR-001 (installments 2-3)",
            reference,
        ),
    ]
    rows = db.scalars(select(CashStatement).order_by(CashStatement.payment_sequence)).all()
    assert [(row.payment_id, row.payment_sequence, row.source_line) for row in rows] == [
        (first, 1, f"repayment:{first}"),
        (second, 2, f"repayment:{second}"),
    ]
    assert {(row.direction, row.counterparty, row.source_table, row.source_id) for row in rows} == {
        ("debit", "Bank BRI", "loans", loan.id)
    }
    assert {(row.source_amount, row.source_transaction_date, row.source_payment_status) for row in rows} == {
        (D("12000000"), START, "PARTIALLY_PAID")
    }
    # The principal/interest split is kept on the installments...
    loan = reload(db, loan)
    assert [(item.principal_paid, item.interest_paid) for item in loan.installments[:4]] == [
        (D("1000000"), D("120000")),
        (D("1000000"), D("120000")),
        (D("260000"), D("120000")),
        (D("0"), D("0")),
    ]
    # ... and on the repayments, with the same totals.
    assert [(item.principal_amount, item.interest_amount) for item in loan.repayments] == [
        (D("1000000"), D("120000")),
        (D("1260000"), D("240000")),
    ]


def test_partial_repayment_pays_interest_first(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    repay(db, actor, loan, "200000")

    loan = reload(db, loan)
    first = loan.installments[0]
    assert (first.status, first.interest_paid, first.principal_paid) == ("PARTIALLY_PAID", D("120000"), D("80000"))
    assert (loan.repayments[0].interest_amount, loan.repayments[0].principal_amount) == (D("120000"), D("80000"))
    assert summarize(loan).next_installment.sequence == 1
    assert [(row[1], row[2]) for row in statement_rows(db)] == [("LOAN_REPAYMENT", D("200000"))]


def test_repayment_validation(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    for day, amount, field in (
        (TODAY + timedelta(days=1), None, "paid_date"),
        (START - timedelta(days=1), None, "paid_date"),
        (TODAY, D("13440001"), "amount"),  # 12,000,000 + 1,440,000 + 1
    ):
        with pytest.raises(AppError) as error:
            repay_loan(db, loan.id, actor, day, amount, None)
        assert error.value.code == "VALIDATION_ERROR" and field in error.value.errors, field
        db.rollback()


def test_paying_everything_pays_the_loan_off(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    repay(db, actor, loan, "13440000")

    loan = reload(db, loan)
    assert loan.status == "PAID_OFF"
    assert {item.status for item in loan.installments} == {"PAID"}
    assert summarize(loan).total_outstanding == 0
    rebuild_cash_statements(db)
    assert {row.source_payment_status for row in db.scalars(select(CashStatement))} == {"PAID"}
    with pytest.raises(AppError) as error:
        repay_loan(db, loan.id, actor, TODAY, None, None)
    assert error.value.code == "LOAN_PAID_OFF"


def test_void_credits_the_main_account_back_and_reallocates(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    first = repay(db, actor, loan, day=TODAY - timedelta(days=1))
    second = repay(db, actor, loan)
    assert main_amount(db) == D("-2240000")

    void_repayment(db, loan.id, first, actor, "Bank reversed it")
    db.commit()

    assert main_amount(db) == D("-1120000")
    voided = db.scalars(select(MainCashAccountLog).order_by(MainCashAccountLog.id.desc())).first()
    assert (voided.direction, voided.amount, voided.reference) == (
        "credit",
        D("1120000"),
        f"loan_repayments:{first}:loan_repayment_voided",
    )
    loan = reload(db, loan)
    remaining = next(item for item in loan.repayments if item.id == second)
    assert (remaining.first_installment, remaining.last_installment) == (1, 1), "now pays installment 1"
    assert [item.status for item in loan.installments[:2]] == ["PAID", "UNPAID"]
    assert [(row[2], row[3]) for row in statement_rows(db)] == [
        (D("1120000"), "Loan repayment: Bank BRI KUR-001 (installment 1)")
    ]

    void_repayment(db, loan.id, first, actor, "again")  # voiding twice does nothing
    db.commit()
    assert main_amount(db) == D("-1120000")


def test_void_of_the_last_repayment_reopens_a_paid_off_loan(db: Session, actor: User) -> None:
    loan = new_loan(db, actor, term_months=1, principal_amount=D("1000000"))
    repayment = repay(db, actor, loan)
    assert reload(db, loan).status == "PAID_OFF"

    void_repayment(db, loan.id, repayment, actor, "Mistake")
    db.commit()

    assert reload(db, loan).status == "ACTIVE"
    assert statement_rows(db) == []


def test_cancel_only_without_repayments(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    repayment = repay(db, actor, loan)
    with pytest.raises(AppError) as error:
        cancel_loan(db, loan.id, actor, "Entered twice")
    assert error.value.code == "LOAN_HAS_REPAYMENTS"
    db.rollback()

    void_repayment(db, loan.id, repayment, actor, "Mistake")
    cancel_loan(db, loan.id, actor, "Entered twice")
    db.commit()

    loan = reload(db, loan)
    assert (loan.status, loan.cancel_reason) == ("CANCELLED", "Entered twice")
    assert summarize(loan).total_outstanding == 0
    with pytest.raises(AppError) as error:
        repay_loan(db, loan.id, actor, TODAY, None, None)
    assert error.value.code == "LOAN_CANCELLED"


# --- Reconciliation -----------------------------------------------------------------------------


def test_audit_reconciles_loan_repayments(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    kept = repay(db, actor, loan, day=TODAY - timedelta(days=1))
    voided = repay(db, actor, loan)
    void_repayment(db, loan.id, voided, actor, "Mistake")
    db.commit()
    drain()

    clean = audit_main_cash_account(mode="full")
    assert clean.status == "SUCCESS" and clean.discrepancies == 0, "live postings are never reversed"

    # Someone deletes the repayment's ledger entry by hand: the audit posts it again.
    db.execute(delete(MainCashAccountLog).where(MainCashAccountLog.source_id == kept))
    db.commit()
    repaired = audit_main_cash_account(mode="full")

    assert repaired.discrepancies == 1
    assert main_amount(db) == D("-1120000") == log_total(db)
    assert db.scalar(
        select(MainCashAccountLog.reference).where(MainCashAccountLog.reference == f"loan_repayments:{kept}:audit")
    )


def test_repayments_reach_the_statement_only_once_synced(db: Session, actor: User) -> None:
    """Repayments no longer sync cash_statements live (see app.features.cash_statement_service);
    syncing the statement (nightly app.scripts.sync_cash_statements; here the full rebuild) is what
    catches them all up, in one pass, whatever happened to them since."""
    loan = new_loan(db, actor)
    repay(db, actor, loan, day=TODAY - timedelta(days=2))
    voided = repay(db, actor, loan, "500000", day=TODAY - timedelta(days=1))
    repay(db, actor, loan, "700000")
    void_repayment(db, loan.id, voided, actor, "Mistake")
    db.commit()

    assert db.scalars(select(CashStatement)).all() == [], "not synced live"
    assert rebuild_cash_statements(db).plan.inserted == 2, "one row per non-voided repayment"
    assert plan_sync(db).changed is False, "re-running it changes nothing"


def test_constraints(db: Session, actor: User) -> None:
    loan = new_loan(db, actor)
    with pytest.raises(IntegrityError):
        db.execute(
            insert(LoanInstallment).values(
                loan_id=loan.id,
                sequence=1,
                due_date=TODAY,
                principal_amount=1,
                interest_amount=0,
                principal_paid=0,
                interest_paid=0,
                status="UNPAID",
            )
        )
    db.rollback()
    long_row = loan_data() | {"created_by": actor.id, "status": "ACTIVE"}
    short_shape = {
        "loan_type": "SHORT_TERM",
        "annual_interest_rate": None,
        "interest_method": None,
        "term_months": None,
    }
    for invalid in (
        long_row | {"loan_type": "MEDIUM_TERM"},
        long_row | {"interest_amount": D("1")},  # a long-term loan has a rate, not an amount
        long_row | {"loan_type": "SHORT_TERM", "interest_amount": D("1")},  # a short-term one has no rate
        long_row | short_shape | {"interest_amount": D("-1")},
    ):
        with pytest.raises(IntegrityError):
            db.execute(insert(Loan).values(**invalid))
        db.rollback()
    db.execute(insert(Loan).values(**(long_row | short_shape | {"interest_amount": D("0")})))  # valid
    db.rollback()


# --- API ----------------------------------------------------------------------------------------


def login(client: TestClient, user: User, password: str) -> None:
    response = client.post(
        "/api/v1/auth/login",
        headers={"X-FM-API-Key": os.environ["API_KEY"]},
        json={"email": user.email, "password": password},
    )
    assert response.status_code == 200
    client.headers.update(
        {"X-FM-API-Key": os.environ["API_KEY"], "Authorization": f"Bearer {response.json()['data']['access_token']}"}
    )


@pytest.fixture
def client(actor: User, actor_password: str) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        login(test_client, actor, actor_password)
        yield test_client


def api_loan(**overrides: object) -> dict[str, object]:
    return {
        "lender_id": LENDER["Bank BRI"],
        "reference_number": "KUR-001",
        "loan_type": "LONG_TERM",
        "principal_amount": "6000000",
        "annual_interest_rate": "9.5",
        "interest_method": "ANNUITY",
        "term_months": 6,
        "start_date": START.isoformat(),
        "first_due_date": (START + timedelta(days=30)).isoformat(),
        "notes": "Working capital",
    } | overrides


def api_short_loan(**overrides: object) -> dict[str, object]:
    return {
        "lender_id": LENDER["Koperasi Maju"],
        "loan_type": "SHORT_TERM",
        "principal_amount": "5000000",
        "interest_amount": "150000",
        "start_date": START.isoformat(),
        "due_date": (START + timedelta(days=7)).isoformat(),
    } | overrides


def test_api_loan_lifecycle(db: Session, client: TestClient) -> None:
    created = client.post("/api/v1/loans", json=api_loan())
    assert created.status_code == 201
    loan = created.json()["data"]
    assert (loan["loan_type"], loan["status"], loan["annual_interest_rate"]) == ("LONG_TERM", "ACTIVE", "9.5000")
    assert loan["interest_amount"] is None
    assert len(loan["installments"]) == 6
    assert loan["summary"]["principal_outstanding"] == "6000000"
    assert loan["maturity_date"] == loan["installments"][-1]["due_date"]
    first_due = loan["installments"][0]["total_amount"]
    assert loan["summary"]["next_installment"] == {
        "sequence": 1,
        "due_date": loan["installments"][0]["due_date"],
        "amount_due": first_due,
    }
    loan_id = loan["id"]

    repaid = client.post(f"/api/v1/loans/{loan_id}/repayments", json={"paid_date": TODAY.isoformat()})
    assert repaid.status_code == 201
    body = repaid.json()["data"]
    (repayment,) = body["repayments"]
    assert repayment["amount"] == first_due
    assert repayment["main_cash_account_reference"] == f"loan_repayments:{repayment['id']}:loan_repaid"
    assert body["installments"][0]["status"] == "PAID"
    assert [log["action"] for log in body["audit_logs"]] == ["loan_repaid", "loan_created"]

    # Posted to the main account by the background task after the response.
    account = client.get("/api/v1/main-cash-account").json()["data"]
    assert account["current_amount"] == f"-{first_due}"
    logs = client.get("/api/v1/main-cash-account/logs", params={"source": "loan_repayments"}).json()
    assert [item["source"]["type"] for item in logs["data"]] == ["loan_repayments"]

    # cash_statements is refreshed once a night (app.scripts.sync_cash_statements), not live.
    empty = client.get("/api/v1/cash-statements", params={"reference": f"loans:{loan_id}"}).json()
    assert empty["data"] == []
    rebuild_cash_statements(db)
    statements = client.get("/api/v1/cash-statements", params={"reference": f"loans:{loan_id}"}).json()
    assert [(row["category"], row["debit"]) for row in statements["data"]] == [("LOAN_REPAYMENT", first_due)]
    assert client.get("/api/v1/cash-statements", params={"source_table": "loans"}).json()["meta"]["total"] == 1

    too_small = client.patch(f"/api/v1/loans/{loan_id}", json=api_loan(principal_amount="100000", term_months=1))
    assert too_small.status_code == 422
    assert "already repaid" in too_small.json()["errors"]["principal_amount"][0]

    voided = client.patch(f"/api/v1/loans/{loan_id}/repayments/{repayment['id']}/void", json={"reason": "Wrong loan"})
    assert voided.status_code == 200
    assert voided.json()["data"]["repayments"][0]["void_reason"] == "Wrong loan"
    assert client.get("/api/v1/main-cash-account").json()["data"]["current_amount"] == "0"

    edited = client.patch(f"/api/v1/loans/{loan_id}", json=api_loan(term_months=3))
    assert edited.status_code == 200 and len(edited.json()["data"]["installments"]) == 3
    cancelled = client.post(f"/api/v1/loans/{loan_id}/cancel", json={"reason": "Test entry"})
    assert cancelled.json()["data"]["status"] == "CANCELLED"
    assert client.patch(f"/api/v1/loans/{loan_id}", json=api_loan()).json()["code"] == "LOAN_CANCELLED"


def test_api_short_term_loan(db: Session, client: TestClient) -> None:
    created = client.post("/api/v1/loans", json=api_short_loan())
    assert created.status_code == 201
    loan = created.json()["data"]
    due = (START + timedelta(days=7)).isoformat()
    assert {
        key: loan[key]
        for key in ("loan_type", "interest_amount", "annual_interest_rate", "interest_method", "term_months")
    } == {
        "loan_type": "SHORT_TERM",
        "interest_amount": "150000",
        "annual_interest_rate": None,
        "interest_method": None,
        "term_months": None,
    }
    assert (loan["first_due_date"], loan["maturity_date"]) == (due, due)
    assert [
        (item["due_date"], item["principal_amount"], item["interest_amount"], item["total_amount"])
        for item in loan["installments"]
    ] == [(due, "5000000", "150000", "5150000")]
    assert loan["summary"]["total_payable"] == "5150000"

    paid = client.post(
        f"/api/v1/loans/{loan['id']}/repayments", json={"paid_date": TODAY.isoformat(), "amount": "1000000"}
    )
    body = paid.json()["data"]
    assert body["installments"][0]["status"] == "PARTIALLY_PAID"
    assert body["summary"]["next_installment"]["amount_due"] == "4150000"
    assert (body["repayments"][0]["principal_amount"], body["repayments"][0]["interest_amount"]) == (
        "850000",
        "150000",
    )
    assert (body["installments"][0]["interest_paid"], body["installments"][0]["principal_paid"]) == ("150000", "850000")

    edited = client.patch(
        f"/api/v1/loans/{loan['id']}", json=api_short_loan(interest_amount="0", due_date=TODAY.isoformat())
    )
    assert edited.status_code == 200
    assert edited.json()["data"]["installments"][0]["due_date"] == TODAY.isoformat()
    # 800,000 + 150,000 is less than the 1,000,000 already repaid.
    too_small = client.patch(f"/api/v1/loans/{loan['id']}", json=api_short_loan(principal_amount="800000"))
    assert too_small.status_code == 422


def test_api_list_filters_and_summary(db: Session, client: TestClient) -> None:
    short = client.post("/api/v1/loans", json=api_short_loan()).json()["data"]
    client.post(
        "/api/v1/loans", json=api_loan(lender_id=LENDER["Bank Mandiri"], principal_amount="24000000", term_months=24)
    )
    client.post(f"/api/v1/loans/{short['id']}/repayments", json={"paid_date": TODAY.isoformat(), "amount": "1000000"})

    listed = client.get("/api/v1/loans").json()
    assert listed["meta"]["total"] == 2
    assert "installments" not in listed["data"][0], "the list only carries the summary"
    assert client.get("/api/v1/loans", params={"loan_type": "LONG_TERM"}).json()["data"][0]["lender"] == {
        "id": LENDER["Bank Mandiri"],
        "name": "Bank Mandiri",
    }
    assert client.get("/api/v1/loans", params={"lender_id": LENDER["Bank Mandiri"]}).json()["meta"]["total"] == 1
    assert client.get("/api/v1/loans", params={"status": "PAID_OFF"}).json()["meta"]["total"] == 0

    summary = client.get("/api/v1/loans/summary").json()["data"]
    assert summary["total"]["active_loans"] == 2
    assert summary["by_type"]["SHORT_TERM"]["active_loans"] == 1
    assert summary["by_type"]["LONG_TERM"]["principal_outstanding"] == "24000000"
    short_outstanding = D(client.get(f"/api/v1/loans/{short['id']}").json()["data"]["summary"]["total_outstanding"])
    assert D(summary["by_type"]["SHORT_TERM"]["total_outstanding"]) == short_outstanding
    assert summary["total"]["overdue_installments"] >= 1  # START is 120 days ago


def test_api_validation_and_permissions(db: Session, client: TestClient, actor: User) -> None:
    assert client.get("/api/v1/loans/999999").status_code == 404
    rate_fields = {"annual_interest_rate": "9.5", "interest_method": "FLAT", "term_months": 1}
    for payload in (
        api_loan(loan_type="MEDIUM_TERM"),
        api_loan(interest_method="EFFECTIVE"),
        api_loan(principal_amount="0"),
        api_loan(annual_interest_rate="101"),
        api_loan(term_months=0),
        api_loan(interest_amount="100000"),  # a long-term loan has a rate, not an amount
        api_short_loan(interest_amount="-1"),
        api_short_loan(due_date=None),
        api_short_loan(**rate_fields),  # a short-term loan has a flat amount, not a rate
        {key: value for key, value in api_short_loan().items() if key != "interest_amount"},
    ):
        assert client.post("/api/v1/loans", json=payload).status_code == 422, payload

    loan = client.post("/api/v1/loans", json=api_loan()).json()["data"]
    client.post(f"/api/v1/loans/{loan['id']}/repayments", json={"paid_date": TODAY.isoformat(), "amount": "1500000"})

    # Demote the logged-in user (same approach as the main cash account tests).
    db.execute(update(User).where(User.id == actor.id).values(role="ADMIN"))
    db.commit()
    assert client.get("/api/v1/loans").status_code == 200, "any active user can read"
    assert client.get("/api/v1/loans/summary").status_code == 200
    detail = client.get(f"/api/v1/loans/{loan['id']}")
    assert detail.status_code == 200
    schedule = detail.json()["data"]
    assert [item["status"] for item in schedule["installments"][:3]] == ["PAID", "PARTIALLY_PAID", "UNPAID"]
    assert [item["amount"] for item in schedule["repayments"]] == ["1500000"]
    base = f"/api/v1/loans/{loan['id']}"
    for method, path, payload in (
        ("post", "/api/v1/loans", api_loan()),
        ("patch", base, api_loan()),
        ("post", f"{base}/cancel", {"reason": "x"}),
        ("post", f"{base}/repayments", {"paid_date": TODAY.isoformat()}),
        ("patch", f"{base}/repayments/{schedule['repayments'][0]['id']}/void", {"reason": "x"}),
    ):
        assert getattr(client, method)(path, json=payload).status_code == 403, path
