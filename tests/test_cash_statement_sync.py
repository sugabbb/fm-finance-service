"""Checks for the nightly, month-scoped cash statement sync and its run history.

Rules under test (see ``sync_period``/``period_scope`` in app/features/cash_statement_service.py,
app/features/cash_statement_sync.py and app/scripts/sync_cash_statements.py):

- a run syncs one month: the transactions whose money moved in it, that were changed during it
  (plus a one-day overlap), or that already have a row dated in it - and all of their rows, so
  numbering across months stays right. Every other transaction is left alone (the full backfill
  still covers the whole history);
- by default the month of the business day that just ended (the run at 00:00 on the 1st closes
  the previous month);
- every run, including dry and failed runs, is recorded in ``cash_statement_sync_runs``; the
  backfill is not.

Data of an "old" month is written with old timestamps, as if it had been recorded back then, so
only the rules above can bring it into scope.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    CashStatement,
    CashStatementSyncRun,
    Lender,
    Loan,
    LoanRepayment,
    MainCashAccountLog,
    OperationalCashflow,
    OperationalCashflowPayment,
    User,
)
from app.features import cash_statement_sync
from app.features.cash_statement_service import (
    CASHFLOW_SOURCE,
    LOAN_SOURCE,
    LOG_SOURCE,
    Period,
    nightly_period,
    rebuild_cash_statements,
    sync_period,
)
from app.features.cash_statement_sync import run_sync
from app.features.common import today_local
from app.features.finance_service import void_operational_payment
from app.features.lender_service import update_lender
from app.features.loan_service import create_loan, repay_loan
from app.features.main_cash_account_service import adjust
from app.scripts.backfill_cash_statements import main as backfill_main
from app.scripts.sync_cash_statements import main

CURRENT = Period.month_of(today_local())
# Far enough back that its data is outside the current month and its one-day overlap.
OLD = Period.month_of(CURRENT.start - timedelta(days=70))


def at(day: date) -> datetime:
    """Noon of ``day`` in APP_TIMEZONE."""
    return datetime.combine(day, time(12), tzinfo=settings.timezone)


OLD_DAY = OLD.start + timedelta(days=9)
OLD_TS = at(OLD_DAY)


# --- Helpers ------------------------------------------------------------------------------------


def cashflow(
    db: Session, actor: User, amount: str, payments: list[tuple[date, str]], touched: datetime = OLD_TS
) -> OperationalCashflow:
    """An approved UTILITY cashflow with its payments, recorded (created/updated) at ``touched``."""
    paid = sum((Decimal(value) for _, value in payments), Decimal("0"))
    status = "UNPAID" if not payments else ("PAID" if paid >= Decimal(amount) else "PARTIALLY_PAID")
    row = OperationalCashflow(
        cashflow_type="UTILITY",
        direction="OUTFLOW",
        transaction_date=payments[0][0] if payments else OLD_DAY,
        amount_idr=Decimal(amount),
        approval_status="APPROVED",
        payment_status=status,
        created_by=actor.id,
        created_at=touched,
        updated_at=touched,
    )
    db.add(row)
    db.flush()
    for day, value in payments:
        db.add(
            OperationalCashflowPayment(
                operational_cashflow_id=row.id,
                paid_date=day,
                amount_idr=Decimal(value),
                payment_mode="PARTIAL",
                created_by=actor.id,
                created_at=touched,
                updated_at=touched,
            )
        )
    db.commit()
    return row


def lines(db: Session) -> list[tuple[date, Decimal, str]]:
    db.expire_all()
    rows = db.scalars(select(CashStatement).order_by(CashStatement.statement_date, CashStatement.id)).all()
    return [(row.statement_date, Decimal(row.amount), row.description) for row in rows]


def runs(db: Session) -> list[CashStatementSyncRun]:
    db.expire_all()
    return list(db.scalars(select(CashStatementSyncRun).order_by(CashStatementSyncRun.id)).all())


# --- Period -------------------------------------------------------------------------------------


def test_period_and_the_month_a_nightly_run_syncs() -> None:
    november = Period(date(2026, 11, 1))
    assert (november.end, november.next_start, november.label) == (date(2026, 11, 30), date(2026, 12, 1), "2026-11")
    assert Period.parse("2028-02").end == date(2028, 2, 29)
    assert Period.parse("2026-12").next_start == date(2027, 1, 1)
    assert november.changed_since == datetime(2026, 10, 31, tzinfo=settings.timezone), "one day of overlap"
    assert november.ends_at == datetime(2026, 12, 1, tzinfo=settings.timezone)

    # The month of the job's start time minus 30 minutes, in APP_TIMEZONE.
    def local(*args: int) -> datetime:
        return datetime(*args, tzinfo=settings.timezone)

    assert nightly_period(local(2026, 11, 17, 0, 0)) == november
    assert nightly_period(local(2026, 12, 1, 0, 0)) == november, "00:00 on the 1st -> 23:30 the day before"
    assert nightly_period(local(2026, 12, 1, 0, 29)) == november, "a run starting a little late too"
    assert nightly_period(local(2026, 12, 1, 0, 30)) == Period(date(2026, 12, 1))
    assert nightly_period(local(2027, 1, 1, 0, 0)) == Period(date(2026, 12, 1)), "across the year"
    assert nightly_period(local(2026, 11, 30, 23, 59)) == november
    # The start time is read in APP_TIMEZONE whatever timezone it is given in.
    assert nightly_period(local(2026, 12, 1, 0, 0).astimezone(UTC)) == november
    with pytest.raises(ValueError):
        nightly_period(datetime(2026, 12, 1))  # naive: ambiguous

    for bad in ("2026-13", "2026", "November"):
        with pytest.raises(ValueError):
            Period.parse(bad)
    with pytest.raises(ValueError):
        Period(date(2026, 11, 2))


# --- Scope: only the month's data ---------------------------------------------------------------


def test_only_the_months_transactions_are_synced(db: Session, actor: User) -> None:
    old = cashflow(db, actor, "100000", [(OLD_DAY, "100000")])
    current = cashflow(db, actor, "250000", [(CURRENT.start, "250000")])  # recorded back then, paid this month

    result = sync_period(db, CURRENT)

    assert result.scope.of(CASHFLOW_SOURCE) == [current.id], "the old month is not even looked at"
    assert (result.plan.inserted, result.plan.updated, result.plan.deleted) == (1, 0, 0)
    assert lines(db) == [(CURRENT.start, Decimal("250000"), "Utility")]
    assert (result.totals.rows, result.totals.debit, result.totals.credit) == (1, Decimal("250000"), Decimal("0"))

    # The full backfill still covers the whole history.
    assert rebuild_cash_statements(db).plan.inserted == 1
    assert [row[0] for row in lines(db)] == [OLD_DAY, CURRENT.start]
    assert old.id not in result.scope.of(CASHFLOW_SOURCE)


def test_a_transaction_spanning_months_is_rebuilt_whole(db: Session, actor: User) -> None:
    cashflow(db, actor, "300000", [(OLD_DAY, "100000"), (CURRENT.start, "200000")])

    sync_period(db, CURRENT)

    # Numbered over all of its payments, and the old month's row is written too.
    assert lines(db) == [
        (OLD_DAY, Decimal("100000"), "Utility (partial payment 1)"),
        (CURRENT.start, Decimal("200000"), "Utility (final payment 2)"),
    ]


def test_a_change_made_this_month_to_an_older_transaction_is_synced(db: Session, actor: User) -> None:
    voided = cashflow(db, actor, "300000", [(OLD_DAY, "100000"), (OLD_DAY + timedelta(days=1), "50000")])
    untouched = cashflow(db, actor, "70000", [(OLD_DAY, "70000")])
    rebuild_cash_statements(db)
    # Drift on a transaction nobody touched this month: not this job's business.
    db.execute(
        update(CashStatement).where(CashStatement.source_id == untouched.id).values(description="stale, on purpose")
    )
    db.commit()

    # This month, someone voids the first (old) payment.
    void_operational_payment(db, voided, voided.payments[0], actor, "Recorded twice")
    db.commit()
    result = sync_period(db, CURRENT)

    assert result.scope.of(CASHFLOW_SOURCE) == [voided.id]
    assert (result.plan.updated, result.plan.deleted) == (1, 1)
    assert lines(db) == [
        (OLD_DAY, Decimal("70000"), "stale, on purpose"),
        (OLD_DAY + timedelta(days=1), Decimal("50000"), "Utility (partial payment 1)"),
    ]
    assert (result.totals.rows, result.totals.debit) == (0, Decimal("0")), "nothing dated this month"


def test_a_changed_transaction_just_before_the_month_is_in_the_overlap(db: Session, actor: User) -> None:
    """A change stamped shortly before midnight of the 1st is caught by the new month's runs."""
    late = cashflow(db, actor, "40000", [(OLD_DAY, "40000")], touched=CURRENT.starts_at - timedelta(minutes=5))
    too_early = cashflow(db, actor, "60000", [(OLD_DAY, "60000")], touched=CURRENT.changed_since - timedelta(hours=1))

    result = sync_period(db, CURRENT)

    assert result.scope.of(CASHFLOW_SOURCE) == [late.id]
    assert too_early.id not in result.scope.of(CASHFLOW_SOURCE)


def test_rows_of_the_month_without_a_source_any_more_are_removed(db: Session) -> None:
    db.execute(
        insert(CashStatement).values(
            statement_date=CURRENT.start,
            direction="debit",
            amount=1,
            category="UTILITY",
            description="source deleted by hand",
            reference="operational_cashflows:999999",
            source_table="operational_cashflows",
            source_id=999999,
            source_line="paid",
            source_transaction_date=CURRENT.start,
            source_amount=1,
        )
    )
    db.commit()

    result = sync_period(db, CURRENT)

    assert result.plan.deleted == 1
    assert lines(db) == []


def test_manual_adjustments_posted_in_the_month(db: Session, actor: User) -> None:
    new = adjust(db, "credit", Decimal("5000000"), "Opening", actor)
    db.commit()
    old_id = db.scalar(
        insert(MainCashAccountLog)
        .values(
            main_cash_account_id=new.main_cash_account_id,
            amount=Decimal("1000"),
            direction="debit",
            reference="manual:Old bank fee",
            created_at=OLD_TS,
        )
        .returning(MainCashAccountLog.id)
    )
    db.commit()

    result = sync_period(db, CURRENT)

    assert result.scope.of(LOG_SOURCE) == [new.id]
    assert old_id not in result.scope.of(LOG_SOURCE)
    assert [row[2] for row in lines(db)] == ["Main account adjustment: Opening"]
    assert result.totals.credit == Decimal("5000000")


def test_renaming_a_lender_refreshes_its_older_loans(db: Session, actor: User) -> None:
    lender = Lender(name="Bank Lama", is_active=True, created_at=OLD_TS, updated_at=OLD_TS)
    db.add(lender)
    db.commit()
    loan = create_loan(
        db,
        {
            "lender_id": lender.id,
            "reference_number": None,
            "loan_type": "SHORT_TERM",
            "principal_amount": Decimal("1000000"),
            "interest_amount": Decimal("0"),
            "start_date": OLD.start,
            "due_date": OLD.start + timedelta(days=7),
            "notes": None,
        },
        actor,
    )
    db.commit()
    repay_loan(db, loan.id, actor, OLD_DAY, None, None)
    db.commit()
    # As if all of it had been recorded back then.
    db.execute(update(Loan).where(Loan.id == loan.id).values(updated_at=OLD_TS, created_at=OLD_TS))
    db.execute(update(LoanRepayment).where(LoanRepayment.loan_id == loan.id).values(updated_at=OLD_TS))
    db.commit()
    rebuild_cash_statements(db)
    assert sync_period(db, CURRENT).scope.of(LOAN_SOURCE) == [], "nothing about this loan happened this month"

    update_lender(db, lender.id, {"name": "Bank Baru", "is_active": True}, actor)
    db.commit()
    result = sync_period(db, CURRENT)

    assert result.scope.of(LOAN_SOURCE) == [loan.id]
    assert result.plan.updated == 1
    assert lines(db) == [(OLD_DAY, Decimal("1000000"), "Loan repayment: Bank Baru")]


# --- Run history --------------------------------------------------------------------------------


def test_a_run_is_recorded(db: Session, actor: User) -> None:
    cashflow(db, actor, "250000", [(CURRENT.start, "250000")])

    outcome = run_sync(period=CURRENT, session=db)
    again = run_sync(period=CURRENT, session=db)

    first, second = runs(db)
    assert (first.id, second.id) == (outcome.run_id, again.run_id)
    assert (first.status, first.dry_run, first.error) == ("SUCCESS", False, None)
    assert (first.period_start, first.period_end) == (CURRENT.start, CURRENT.end)
    assert first.changed_since == CURRENT.changed_since
    assert first.started_at <= first.finished_at
    assert (first.cashflows_checked, first.revenues_checked, first.adjustments_checked, first.loans_checked) == (
        1,
        0,
        0,
        0,
    )
    assert first.transactions_checked == 1
    assert (first.rows_inserted, first.rows_updated, first.rows_deleted) == (1, 0, 0)
    assert (first.period_rows, first.period_credit, first.period_debit) == (1, Decimal("0"), Decimal("250000"))
    assert (second.rows_inserted, second.rows_updated, second.rows_deleted) == (0, 0, 0), "idempotent"
    assert second.period_rows == 1


def test_a_dry_run_is_recorded_but_writes_nothing(db: Session, actor: User) -> None:
    cashflow(db, actor, "250000", [(CURRENT.start, "250000")])

    run_sync(period=CURRENT, dry_run=True, session=db)

    (run,) = runs(db)
    assert (run.status, run.dry_run, run.rows_inserted, run.period_rows) == ("SUCCESS", True, 1, 1)
    assert lines(db) == []


def test_a_failed_run_is_recorded(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database on fire")

    monkeypatch.setattr(cash_statement_sync, "sync_period", broken)

    with pytest.raises(RuntimeError):
        run_sync(period=CURRENT, session=db)

    (run,) = runs(db)
    assert (run.status, run.error) == ("FAILED", "RuntimeError: database on fire")
    assert run.finished_at is not None
    assert run.rows_inserted == 0


def test_the_default_month_is_read_30_minutes_before_the_start(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # The cron run at 00:00 on the 1st of the next month syncs this month, not the empty new one.
    started = CURRENT.ends_at
    monkeypatch.setattr(cash_statement_sync, "now_utc", lambda: started.astimezone(UTC))

    outcome = run_sync(session=db)

    assert outcome.result.period == CURRENT
    (run,) = runs(db)
    assert (run.period_start, run.started_at) == (CURRENT.start, started)


# --- Commands -----------------------------------------------------------------------------------


def test_cli(db: Session, actor: User, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="app.scripts.sync_cash_statements")
    cashflow(db, actor, "100000", [(OLD_DAY, "100000")])
    label = f"cash statements {OLD.label}"

    assert main(["--month", OLD.label, "--dry-run"]) == 0
    assert f"{label} (run " in caplog.text
    assert "would change: 1 inserted, 0 updated, 0 deleted" in caplog.text
    assert lines(db) == []
    caplog.clear()

    assert main(["--month", OLD.label]) == 0
    assert "changed: 1 inserted, 0 updated, 0 deleted" in caplog.text
    assert "1 rows, credit 0, debit 100000" in caplog.text
    assert len(lines(db)) == 1
    caplog.clear()

    assert main(["--month", OLD.label]) == 0
    assert "already up to date; nothing to change" in caplog.text

    assert [(run.period_start, run.dry_run, run.rows_inserted) for run in runs(db)] == [
        (OLD.start, True, 1),
        (OLD.start, False, 1),
        (OLD.start, False, 0),
    ]

    with pytest.raises(SystemExit) as error:
        main(["--month", "2026-13"])
    assert error.value.code == 2


def test_cli_failure_exit_code(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cash_statement_sync, "sync_period", broken)

    assert main(["--month", CURRENT.label]) == 1
    assert [run.status for run in runs(db)] == ["FAILED"]


def test_the_backfill_is_unchanged_and_not_recorded(db: Session, actor: User) -> None:
    cashflow(db, actor, "100000", [(OLD_DAY, "100000")])
    cashflow(db, actor, "250000", [(CURRENT.start, "250000")])

    assert backfill_main([]) == 0

    assert len(lines(db)) == 2, "the whole history, whatever the month"
    assert runs(db) == []


def test_old_timestamps_are_really_old() -> None:
    """Guards the fixtures above: the old month's data must be outside the current run's window."""
    assert OLD_TS < CURRENT.changed_since
    assert OLD.end < CURRENT.start
    assert OLD_TS.astimezone(UTC) < datetime.now(UTC)
