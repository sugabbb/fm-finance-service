"""Checks for the monthly cut-off (app/features/monthly_cutoff_service.py, app/scripts/monthly_cutoff.py).

One realistic closed month (``MONTH``, the month before the current one) is built through the
finance/loan services, with activity before it (opening balances, an older expense and older
unpaid sales), during it, and after it (this month: payments that must not change the closed
month). Every figure of the cut-off is then checked against amounts worked out by hand:

- actual cash = opening (statement before the month + go-live opening balances) + opening
  balances of the month + credits - debits, never adding the main account/cash on hand balances;
- the cash-basis P&L by statement source, with only the interest of loan repayments and the
  manual adjustments/loan principal kept out of it (``NON_PL``), reconciling with the cash movement;
- loans and receivables as of the month's last day;
- snapshots: a re-run saves a new one and moves ``is_latest``; failures are recorded; dry runs
  save nothing; an open month cannot be closed.
"""

import os
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import finance_reader
from app.core.errors import AppError
from app.db.models import (
    CashStatement,
    CashStatementSyncRun,
    FinancialOpeningBalance,
    Lender,
    MainCashAccount,
    MainCashAccountLog,
    MonthlyCutoff,
    MonthlyCutoffCategory,
    MonthlyCutoffLoan,
    MonthlyCutoffProfitLoss,
    MonthlyCutoffReceivable,
    OperationalCashflowBalance,
    Revenue,
    RevenueTransferDetail,
    User,
)
from app.features import monthly_cutoff_service
from app.features.cash_statement_service import Period, rebuild_cash_statements
from app.features.cash_statement_sync import run_sync
from app.features.common import today_local
from app.features.finance_service import (
    decide_operational,
    decide_revenue,
    pay_operational,
    pay_revenue,
    save_operational,
    save_revenue,
    submit_operational,
)
from app.features.loan_service import add_months, cancel_loan, create_loan, repay_loan
from app.features.main_cash_account_service import get_or_create_account
from app.features.monthly_cutoff_service import MonthNotClosedError, run_cutoff
from app.main import app
from app.scripts.monthly_cutoff import main

D = Decimal
TODAY = today_local()
CURRENT = Period.month_of(TODAY)
MONTH = Period.month_of(CURRENT.start - timedelta(days=1))  # the closed month under test
OLDER = Period.month_of(MONTH.start - timedelta(days=1))


def day(offset: int) -> date:
    """A day of MONTH."""
    return MONTH.start + timedelta(days=offset)


def at(value: date) -> datetime:
    return datetime.combine(value, time(12), tzinfo=settings.timezone)


# --- Data ---------------------------------------------------------------------------------------


def paid_expense(db: Session, actor: User, cashflow_type: str, amount: str, paid_on: date, **details: str) -> None:
    row = save_operational(
        db,
        {"cashflow_type": cashflow_type, "transaction_date": paid_on, "amount_idr": D(amount), "details": details},
        actor,
    )
    submit_operational(db, row, actor)
    decide_operational(db, row, actor, "APPROVED", None)
    pay_operational(db, row, actor, paid_on, "FULL", None)
    db.commit()


def egg_sale(db: Session, actor: User, customer: str, sold_on: date, method: str = "CASH") -> Revenue:
    """10 kg x 25,000 = 250,000."""
    row = save_revenue(
        db,
        {
            "revenue_type": "EGG_SALE",
            "transaction_date": sold_on,
            "payment_method": method,
            "direct_transfer_date": sold_on if method == "DIRECT_TRANSFER" else None,
            "customer_id": None,
            "new_customer": {"name": customer, "phone": "0812", "customer_type": "WARUNG", "notes": None},
            "details": [
                {
                    "egg_category": "UTUH",
                    "weight_kg": D("10"),
                    "quantity_pieces": 160,
                    "price_per_kg_idr": D("25000"),
                }
            ],
        },
        actor,
    )
    db.commit()
    return row


def manual_log(db: Session, direction: str, amount: str, note: str, posted_on: date) -> None:
    """A manual main account adjustment posted on ``posted_on`` (the API posts them 'now')."""
    account = get_or_create_account(db)
    db.execute(
        insert(MainCashAccountLog).values(
            main_cash_account_id=account.id,
            amount=D(amount),
            direction=direction,
            reference=f"manual:{note}",
            created_at=at(posted_on),
        )
    )
    db.commit()


@pytest.fixture
def month(db: Session, actor: User) -> dict[str, int]:
    """The closed month and its surroundings (see the module docstring); returns a few ids."""
    # Go-live opening balances: one before the month, one during it.
    db.add_all(
        [
            FinancialOpeningBalance(
                account="OPERATIONAL", amount_idr=D("1000000"), effective_date=OLDER.start, created_by=actor.id
            ),
            FinancialOpeningBalance(
                account="REVENUE", amount_idr=D("200000"), effective_date=day(2), created_by=actor.id
            ),
        ]
    )
    db.commit()

    # Before the month: an expense (part of the opening cash) and two sales never paid.
    paid_expense(db, actor, "UTILITY", "100000", OLDER.start + timedelta(days=4), description="Old electricity")
    egg_sale(db, actor, "Agen Jaya", MONTH.end - timedelta(days=45))  # 45 days old at month end
    egg_sale(db, actor, "Toko Lama", MONTH.end - timedelta(days=100))  # 100 days old

    # The month.
    paid_expense(db, actor, "UTILITY", "300000", day(5), description="Electricity")
    paid_expense(db, actor, "OTHER", "50000", day(6), description="Repairs")
    bu_sri = egg_sale(db, actor, "Warung Bu Sri", day(1))
    pay_revenue(db, bu_sri, actor, day(3), "PARTIAL", D("100000"))
    db.commit()
    egg_sale(db, actor, "Pak Transfer", day(2), method="DIRECT_TRANSFER")  # waiting for approval
    rejected = egg_sale(db, actor, "Pak Ditolak", day(2), method="DIRECT_TRANSFER")
    decide_revenue(db, rejected, actor, "REJECTED", "Not received", False)
    db.commit()
    other = save_revenue(
        db,
        {
            "revenue_type": "OTHER",
            "transaction_date": day(7),
            "payment_method": "CASH",
            "amount_idr": D("75000"),
            "details": {"notes": "Sold used sacks"},
        },
        actor,
    )
    db.commit()
    pay_revenue(db, other, actor, day(7), "FULL", None)
    db.commit()
    # A main account transfer approved during the month (recorded directly: the API approves
    # transfers "today"): only its 6,500 fee is a statement row.
    transfer = Revenue(
        revenue_type="MAIN_ACCOUNT_TRANSFER",
        direction="OUTFLOW",
        transaction_date=day(8),
        amount_idr=D("993500"),
        approval_status="APPROVED",
        payment_status="PAID",
        paid_at=at(day(8)),
        created_by=actor.id,
    )
    db.add(transfer)
    db.flush()
    db.add(RevenueTransferDetail(revenue_id=transfer.id, gross_amount_idr=D("1000000"), payment_cost_idr=D("6500")))
    db.commit()
    manual_log(db, "credit", "5000000", "Loan proceeds", day(9))
    manual_log(db, "debit", "2500", "Bank admin fee", day(9))

    # A loan: 3,000,000 over 3 months at 12 % flat = 1,030,000 a month (30,000 interest), the first
    # installment due during the month and only partly paid (500,000 = 30,000 interest + 470,000
    # principal) by its end.
    lender = Lender(name="Bank BRI", is_active=True)
    db.add(lender)
    db.commit()
    loan = create_loan(
        db,
        {
            "lender_id": lender.id,
            "reference_number": "KUR-7",
            "loan_type": "LONG_TERM",
            "principal_amount": D("3000000"),
            "annual_interest_rate": D("12"),
            "interest_method": "FLAT",
            "term_months": 3,
            "start_date": OLDER.start,
            "first_due_date": day(9),
            "notes": None,
        },
        actor,
    )
    db.commit()
    repay_loan(db, loan.id, actor, day(10), D("500000"), None)
    db.commit()
    # Loans that are not part of the month: cancelled, and started after it.
    for start, cancel in ((OLDER.start, True), (TODAY, False)):
        extra = create_loan(
            db,
            {
                "lender_id": lender.id,
                "reference_number": None,
                "loan_type": "SHORT_TERM",
                "principal_amount": D("1000000"),
                "interest_amount": D("0"),
                "start_date": start,
                "due_date": start + timedelta(days=7),
                "notes": None,
            },
            actor,
        )
        db.commit()
        if cancel:
            cancel_loan(db, extra.id, actor, "Entered twice")
            db.commit()

    # After the month (this month): must not change the closed month.
    pay_revenue(db, bu_sri, actor, TODAY, "FULL", None)  # Bu Sri pays the remaining 150,000
    db.commit()
    repay_loan(db, loan.id, actor, TODAY, D("530000"), None)  # the rest of installment 1
    db.commit()
    paid_expense(db, actor, "UTILITY", "999000", TODAY, description="Next month's bill")

    rebuild_cash_statements(db)  # the whole history on the statement
    return {"loan": loan.id, "lender": lender.id}


def cutoff_rows(db: Session) -> list[MonthlyCutoff]:
    db.expire_all()
    return list(db.scalars(select(MonthlyCutoff).order_by(MonthlyCutoff.id)).all())


def details(db: Session, model: type, cutoff_id: int) -> list:
    return list(db.scalars(select(model).where(model.cutoff_id == cutoff_id).order_by(model.id)).all())


# --- Figures ------------------------------------------------------------------------------------


def test_a_closed_month(db: Session, month: dict[str, int]) -> None:
    outcome = run_cutoff(period=MONTH, session=db)

    (cutoff,) = cutoff_rows(db)
    assert cutoff.id == outcome.cutoff_id
    assert (cutoff.status, cutoff.is_latest, cutoff.error, cutoff.warnings) == ("SUCCESS", True, None, [])
    assert (cutoff.period_start, cutoff.period_end) == (MONTH.start, MONTH.end)
    assert db.get(CashStatementSyncRun, cutoff.sync_run_id).period_start == MONTH.start

    # Actual cash. Opening: 1,000,000 go-live balance - 100,000 older expense.
    assert cutoff.cash_opening == D("900000")
    assert cutoff.opening_balances_in_period == D("200000")
    assert cutoff.cash_in == D("5175000")  # 100,000 eggs + 75,000 other + 5,000,000 loan proceeds
    assert cutoff.cash_out == D("859000")  # 300,000 + 50,000 + 6,500 fee + 500,000 loan + 2,500 bank fee
    assert cutoff.cash_closing == D("5416000") == D("900000") + D("200000") + D("5175000") - D("859000")
    assert cutoff.opening_balances_total == D("1200000")
    assert cutoff.statement_rows == 8

    # Profit and loss (cash basis).
    assert (cutoff.revenue, cutoff.operating_expenses, cutoff.other_expenses, cutoff.finance_costs) == (
        D("175000"),
        D("350000"),
        D("6500"),
        D("30000"),
    )
    assert cutoff.net_profit == D("-211500")
    lines = [(item.section, item.line, item.amount) for item in details(db, MonthlyCutoffProfitLoss, cutoff.id)]
    assert lines == [
        ("REVENUE", "EGG_SALE", D("100000")),
        ("REVENUE", "OTHER", D("75000")),
        ("OPERATING_EXPENSE", "UTILITY", D("300000")),
        ("OPERATING_EXPENSE", "OTHER", D("50000")),
        ("OTHER_EXPENSE", "TRANSFER_FEE", D("6500")),
        ("FINANCE_COST", "LOAN_INTEREST", D("30000")),
        ("NON_PL", "MANUAL_ADJUSTMENT_IN", D("5000000")),
        ("NON_PL", "LOAN_PRINCIPAL", D("470000")),
        ("NON_PL", "MANUAL_ADJUSTMENT_OUT", D("2500")),
    ]
    # The P&L reconciles with the cash movement.
    assert cutoff.net_profit - D("470000") + D("5000000") - D("2500") == cutoff.cash_in - cutoff.cash_out

    # The statement grouped by category.
    categories = [
        (item.direction, item.source_table, item.category, item.rows, item.amount)
        for item in details(db, MonthlyCutoffCategory, cutoff.id)
    ]
    assert categories == [
        ("credit", "main_cash_account_logs", "MANUAL_ADJUSTMENT", 1, D("5000000")),
        ("credit", "revenues", "EGG_SALE", 1, D("100000")),
        ("credit", "revenues", "OTHER", 1, D("75000")),
        ("debit", "loans", "LOAN_REPAYMENT", 1, D("500000")),
        ("debit", "main_cash_account_logs", "MANUAL_ADJUSTMENT", 1, D("2500")),
        ("debit", "operational_cashflows", "OTHER", 1, D("50000")),
        ("debit", "operational_cashflows", "UTILITY", 1, D("300000")),
        ("debit", "revenues", "TRANSFER_FEE", 1, D("6500")),
    ]

    # Loans at the month's end: the repayment made this month does not count yet.
    assert (cutoff.loans_outstanding, cutoff.loan_principal_outstanding, cutoff.loan_interest_outstanding) == (
        1,
        D("2530000"),
        D("60000"),
    )
    assert (cutoff.loan_overdue_amount, cutoff.loan_principal_repaid, cutoff.loan_interest_repaid) == (
        D("530000"),
        D("470000"),
        D("30000"),
    )
    (loan,) = details(db, MonthlyCutoffLoan, cutoff.id)
    assert (loan.loan_id, loan.lender_name, loan.reference_number, loan.loan_type) == (
        month["loan"],
        "Bank BRI",
        "KUR-7",
        "LONG_TERM",
    )
    assert (loan.principal_amount, loan.principal_paid, loan.principal_outstanding) == (
        D("3000000"),
        D("470000"),
        D("2530000"),
    )
    assert (loan.interest_total, loan.interest_paid, loan.interest_outstanding) == (D("90000"), D("30000"), D("60000"))
    assert (loan.overdue_installments, loan.overdue_amount) == (1, D("530000"))
    assert (loan.principal_repaid_in_period, loan.interest_repaid_in_period) == (D("470000"), D("30000"))

    # Receivables at the month's end: Bu Sri's payment made this month does not count yet.
    assert (cutoff.receivable_invoices, cutoff.receivables, cutoff.receivables_pending_verification) == (
        4,
        D("650000"),
        D("250000"),
    )
    receivables = {
        item.customer_name: (
            item.invoices,
            item.outstanding,
            item.aging_0_30,
            item.aging_31_60,
            item.aging_61_90,
            item.aging_over_90,
            item.pending_verification,
        )
        for item in details(db, MonthlyCutoffReceivable, cutoff.id)
    }
    zero = D("0")
    assert receivables == {
        "Warung Bu Sri": (1, D("150000"), D("150000"), zero, zero, zero, zero),
        "Agen Jaya": (1, D("250000"), zero, D("250000"), zero, zero, zero),
        "Toko Lama": (1, D("250000"), zero, zero, zero, D("250000"), zero),
        "Pak Transfer": (1, zero, zero, zero, zero, zero, D("250000")),
    }, "paid sales and the rejected direct transfer are not receivables"


def test_cash_is_not_counted_twice(db: Session, month: dict[str, int]) -> None:
    """The main account and the cash on hand are pockets of the same money: their balances are
    never added to the statement's cash, whatever they hold."""
    db.execute(update(MainCashAccount).values(current_amount=D("999999999")))
    db.execute(
        update(OperationalCashflowBalance)
        .where(OperationalCashflowBalance.type == "cash_on_hand")
        .values(amount=D("888888"))
    )
    db.commit()

    run_cutoff(period=MONTH, session=db)

    assert cutoff_rows(db)[0].cash_closing == D("5416000")


def test_the_month_is_synced_before_it_is_closed(db: Session, actor: User, month: dict[str, int]) -> None:
    """The cut-off first syncs the month, so a statement that is up to a day behind is not closed."""
    db.execute(update(CashStatement).where(CashStatement.category == "TRANSFER_FEE").values(amount=D("1")))  # drift
    db.commit()

    run_cutoff(period=MONTH, session=db)

    assert cutoff_rows(db)[0].other_expenses == D("6500")


def test_a_statement_out_of_line_with_the_loans_is_warned_about(
    db: Session, month: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    sync = run_sync(period=MONTH, session=db)
    db.execute(update(CashStatement).where(CashStatement.category == "LOAN_REPAYMENT").values(amount=D("400000")))
    db.commit()
    # Skip the sync that would repair the row, to see what the checks do with it.
    monkeypatch.setattr(monthly_cutoff_service, "run_sync", lambda **kwargs: SimpleNamespace(run_id=sync.run_id))

    run_cutoff(period=MONTH, session=db)

    (cutoff,) = cutoff_rows(db)
    assert cutoff.status == "SUCCESS", "the figures are still saved"
    assert cutoff.warnings == [
        "loan repayments of the month (500000) differ from the statement's LOAN_REPAYMENT rows (400000)",
        "the P&L does not reconcile with the cash movement: net profit - loan principal + manual adjustments"
        " = 4316000, cash in - cash out = 4416000",
    ]


# --- Snapshots ----------------------------------------------------------------------------------


def test_a_rerun_saves_a_new_snapshot_and_moves_is_latest(db: Session, actor: User, month: dict[str, int]) -> None:
    first = run_cutoff(period=MONTH, session=db)
    # A late entry: a payment of the month recorded after the close.
    agen = db.scalar(select(Revenue).where(Revenue.transaction_date == MONTH.end - timedelta(days=45)))
    pay_revenue(db, agen, actor, day(20), "FULL", None)
    db.commit()

    second = run_cutoff(period=MONTH, session=db)

    old, new = cutoff_rows(db)
    assert (old.id, new.id) == (first.cutoff_id, second.cutoff_id)
    assert (old.is_latest, new.is_latest) == (False, True)
    assert (old.receivables, new.receivables) == (D("650000"), D("400000")), "the earlier figures are kept"
    assert new.revenue == old.revenue + D("250000")
    assert len(details(db, MonthlyCutoffReceivable, old.id)) == 4


def test_a_failed_run_is_recorded(db: Session, month: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    good = run_cutoff(period=MONTH, session=db)

    def broken(*args: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(monthly_cutoff_service, "compute_cutoff", broken)
    with pytest.raises(RuntimeError):
        run_cutoff(period=MONTH, session=db)

    ok, failed = cutoff_rows(db)
    assert (failed.status, failed.is_latest, failed.error) == ("FAILED", False, "RuntimeError: disk full")
    assert failed.sync_run_id is not None and failed.finished_at is not None
    assert details(db, MonthlyCutoffCategory, failed.id) == []
    assert (ok.id, ok.is_latest) == (good.cutoff_id, True), "the last good close stays the reported one"


def test_a_dry_run_saves_nothing(db: Session, month: dict[str, int]) -> None:
    outcome = run_cutoff(period=MONTH, dry_run=True, session=db)

    assert outcome.cutoff_id is None and outcome.dry_run
    assert outcome.figures.cash.closing == D("5416000")
    assert outcome.figures.profit_loss.net_profit == D("-211500")
    assert cutoff_rows(db) == []
    assert db.get(CashStatementSyncRun, outcome.sync_run_id) is not None, "the month's sync still ran"


def test_only_a_closed_month_can_be_cut_off(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(MonthNotClosedError):
        run_cutoff(period=CURRENT, session=db)
    assert cutoff_rows(db) == []

    # The cron run at 00:00 on the 1st closes the month that just ended.
    monkeypatch.setattr(monthly_cutoff_service, "now_utc", lambda: CURRENT.starts_at)
    outcome = run_cutoff(session=db)
    assert outcome.figures.period == MONTH
    assert cutoff_rows(db)[0].period_start == MONTH.start

    # Mid-month, without --month, there is nothing to close.
    monkeypatch.setattr(monthly_cutoff_service, "now_utc", lambda: CURRENT.starts_at + timedelta(days=14))
    with pytest.raises(MonthNotClosedError):
        run_cutoff(session=db)


# --- Command ------------------------------------------------------------------------------------


def test_cli(db: Session, month: dict[str, int], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="app.scripts.monthly_cutoff")
    prefix = f"monthly cut-off {MONTH.label}"

    assert main(["--month", MONTH.label, "--dry-run"]) == 0
    assert f"{prefix}: dry run, not saved" in caplog.text
    assert cutoff_rows(db) == []
    caplog.clear()

    assert main(["--month", MONTH.label]) == 0
    (cutoff,) = cutoff_rows(db)
    assert f"{prefix}: saved as cut-off {cutoff.id}" in caplog.text
    assert f"{prefix}: cash opening 1100000, in 5175000, out 859000, closing 5416000" in caplog.text
    assert f"{prefix}: revenue 175000, net profit -211500" in caplog.text
    assert f"{prefix}: 1 loans owed, principal outstanding 2530000" in caplog.text
    assert f"{prefix}: receivables 650000 from 4 customers" in caplog.text

    assert main(["--month", CURRENT.label]) == 2, "the month has not ended"
    with pytest.raises(SystemExit) as error:
        main(["--month", "2026-13"])
    assert error.value.code == 2


# --- API ----------------------------------------------------------------------------------------

URL = "/api/v1/monthly-cutoffs"


@pytest.fixture
def client(actor: User, actor_password: str) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        login = test_client.post(
            "/api/v1/auth/login",
            headers={"X-FM-API-Key": os.environ["API_KEY"]},
            json={"email": actor.email, "password": actor_password},
        )
        assert login.status_code == 200
        test_client.headers.update(
            {
                "X-FM-API-Key": os.environ["API_KEY"],
                "Authorization": f"Bearer {login.json()['data']['access_token']}",
            }
        )
        yield test_client


def month_params(period: Period = MONTH) -> dict[str, int]:
    return {"year": period.start.year, "month": period.start.month}


def test_api_returns_the_months_close(db: Session, month: dict[str, int], client: TestClient) -> None:
    run_cutoff(period=MONTH, session=db)
    cutoff = cutoff_rows(db)[0]

    response = client.get(URL, params=month_params())

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["id"] == cutoff.id
    assert body["period"] == {
        "year": MONTH.start.year,
        "month": MONTH.start.month,
        "start": MONTH.start.isoformat(),
        "end": MONTH.end.isoformat(),
    }
    assert (body["revision"], body["warnings"]) == (1, [])
    assert body["generated_at"] == cutoff.finished_at.isoformat()
    assert body["cash_statement_sync_run_id"] == cutoff.sync_run_id
    assert body["cash"] == {
        "opening": "900000",
        "opening_balances_in_period": "200000",
        "cash_in": "5175000",
        "cash_out": "859000",
        "closing": "5416000",
        "opening_balances_total": "1200000",
        "statement_rows": 8,
    }
    pl = body["profit_and_loss"]
    assert (pl["basis"], pl["revenue"], pl["operating_expenses"], pl["other_expenses"], pl["finance_costs"]) == (
        "CASH",
        "175000",
        "350000",
        "6500",
        "30000",
    )
    assert pl["net_profit"] == "-211500"
    assert [(item["section"], item["line"], item["amount"]) for item in pl["lines"]][:3] == [
        ("REVENUE", "EGG_SALE", "100000"),
        ("REVENUE", "OTHER", "75000"),
        ("OPERATING_EXPENSE", "UTILITY", "300000"),
    ]
    assert len(pl["lines"]) == 9

    loans = body["loans"]
    assert {key: value for key, value in loans.items() if key != "items"} == {
        "outstanding_count": 1,
        "principal_outstanding": "2530000",
        "interest_outstanding": "60000",
        "overdue_amount": "530000",
        "principal_repaid": "470000",
        "interest_repaid": "30000",
    }
    (loan,) = loans["items"]
    assert loan["lender"] == {"id": month["lender"], "name": "Bank BRI"}
    assert (loan["loan_id"], loan["reference_number"], loan["maturity_date"]) == (
        month["loan"],
        "KUR-7",
        add_months(day(9), 2).isoformat(),  # the third monthly installment
    )
    assert (loan["principal_outstanding"], loan["overdue_installments"], loan["interest_repaid_in_period"]) == (
        "2530000",
        1,
        "30000",
    )

    receivables = body["receivables"]
    assert (receivables["invoices"], receivables["outstanding"], receivables["pending_verification"]) == (
        4,
        "650000",
        "250000",
    )
    by_name = {item["customer"]["name"]: item for item in receivables["customers"]}
    assert by_name["Agen Jaya"]["aging"] == {
        "days_0_30": "0",
        "days_31_60": "250000",
        "days_61_90": "0",
        "over_90_days": "0",
    }
    assert by_name["Pak Transfer"]["pending_verification"] == "250000"

    assert len(body["categories"]) == 8
    assert body["categories"][0] == {
        "direction": "credit",
        "source_table": "main_cash_account_logs",
        "category": "MANUAL_ADJUSTMENT",
        "rows": 1,
        "amount": "5000000",
    }


def test_api_returns_the_latest_close_of_the_month(
    db: Session, actor: User, month: dict[str, int], client: TestClient
) -> None:
    run_cutoff(period=MONTH, session=db)
    agen = db.scalar(select(Revenue).where(Revenue.transaction_date == MONTH.end - timedelta(days=45)))
    pay_revenue(db, agen, actor, day(20), "FULL", None)  # a late entry, then the month is closed again
    db.commit()
    restated = run_cutoff(period=MONTH, session=db)

    body = client.get(URL, params=month_params()).json()["data"]

    assert (body["id"], body["revision"]) == (restated.cutoff_id, 2)
    assert body["receivables"]["outstanding"] == "400000"


def test_api_month_not_closed(
    db: Session, month: dict[str, int], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = client.get(URL, params=month_params())
    assert response.status_code == 404
    assert response.json() == {
        "code": "NOT_FOUND",
        "detail": f"The monthly cut-off of {MONTH.label} was not found: the month has not been closed.",
    }

    # A failed close is not reported either.
    monkeypatch.setattr(monthly_cutoff_service, "compute_cutoff", lambda *args: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        run_cutoff(period=MONTH, session=db)
    assert client.get(URL, params=month_params()).status_code == 404
    assert client.get(URL, params=month_params(CURRENT)).status_code == 404


@pytest.mark.parametrize(
    ("query", "errors"),
    [
        ("", {"year": ["Field required"], "month": ["Field required"]}),
        ("year=2026", {"month": ["Field required"]}),
        ("month=7", {"year": ["Field required"]}),
        ("year=2026&month=13", {"month": ["Input should be less than or equal to 12"]}),
        ("year=2026&month=0", {"month": ["Input should be greater than or equal to 1"]}),
        ("year=1999&month=7", {"year": ["Input should be greater than or equal to 2000"]}),
        ("year=2026&month=july", {"month": ["Input should be a valid integer, unable to parse string as an integer"]}),
        # One call, one month: no second month, no second year.
        ("year=2026&month=7&month=8", {"month": ["Give a single value: one call returns one month."]}),
        ("year=2026&year=2027&month=7", {"year": ["Give a single value: one call returns one month."]}),
    ],
)
def test_api_year_and_month_are_required_and_single(db: Session, client: TestClient, query: str, errors: dict) -> None:
    response = client.get(f"{URL}?{query}")

    assert response.status_code == 422
    assert response.json() == {"code": "VALIDATION_ERROR", "detail": "The request is invalid.", "errors": errors}


def test_api_access(db: Session, actor: User, month: dict[str, int], client: TestClient) -> None:
    run_cutoff(period=MONTH, session=db)

    assert client.get(URL, params=month_params()).status_code == 200, "SUPERADMIN"
    db.execute(update(User).where(User.id == actor.id).values(role="ADMIN"))
    db.commit()
    assert client.get(URL, params=month_params()).status_code == 200, "ADMIN"

    db.execute(update(User).where(User.id == actor.id).values(must_change_password=True))
    db.commit()
    assert client.get(URL, params=month_params()).json()["code"] == "PASSWORD_CHANGE_REQUIRED"

    anonymous = client.get(URL, params=month_params(), headers={"Authorization": ""})
    assert anonymous.status_code == 401


def test_only_the_finance_roles_can_read() -> None:
    """Today ADMIN and SUPERADMIN are all the roles; a role added later gets no access by default."""
    assert finance_reader(SimpleNamespace(role="ADMIN")) and finance_reader(SimpleNamespace(role="SUPERADMIN"))
    with pytest.raises(AppError) as error:
        finance_reader(SimpleNamespace(role="FARM_STAFF"))
    assert error.value.status_code == 403


def test_api_is_documented(client: TestClient) -> None:
    operation = client.get("/openapi.json").json()["paths"][URL]["get"]
    assert operation["security"] == [{"BearerAuth": []}]
    query = {item["name"]: item["required"] for item in operation["parameters"] if item["in"] == "query"}
    assert query == {"year": True, "month": True}
    assert list(client.get("/openapi.json").json()["paths"][URL]) == ["get"], "read only"
