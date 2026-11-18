"""Tests for the manual CLI backfill: python -m app.scripts.backfill_cash_on_hand."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db.models import OperationalCashflow, OperationalCashflowBalance, OperationalCashflowPayment, User
from app.features.cash_balance_service import CASH_ON_HAND, calculate_cash_on_hand, get_balance
from app.features.common import today_local
from app.features.finance_service import (
    decide_operational,
    pay_operational,
    save_operational,
    submit_operational,
    void_operational_payment,
)
from app.scripts.backfill_cash_on_hand import backfill_cash_on_hand, main


def legacy_cashflow(
    db: Session, actor: User, cashflow_type: str, payment_status: str, amount: str, paid: tuple[str, ...] = ()
) -> None:
    """Insert a row straight into operational_cashflows, bypassing the service layer, to mimic
    history that existed before the balance table (the live sync never saw these rows). ``paid``
    are its payments; one prefixed with ``-`` is stored as voided."""
    topup = cashflow_type == "MAIN_ACCOUNT_TOPUP"
    row = OperationalCashflow(
        cashflow_type=cashflow_type,
        direction="INFLOW" if topup else "OUTFLOW",
        transaction_date=date(2026, 1, 15),
        amount_idr=Decimal(amount),
        approval_status="NOT_REQUIRED" if topup else "APPROVED",
        payment_status=payment_status,
        created_by=actor.id,
    )
    db.add(row)
    db.flush()
    for value in paid:
        voided = value.startswith("-")
        db.add(
            OperationalCashflowPayment(
                operational_cashflow_id=row.id,
                paid_date=date(2026, 1, 16),
                amount_idr=Decimal(value.lstrip("-")),
                payment_mode="PARTIAL",
                created_by=actor.id,
                voided_at=datetime(2026, 1, 17, tzinfo=UTC) if voided else None,
                voided_by=actor.id if voided else None,
                void_reason="voided" if voided else None,
            )
        )


@pytest.fixture
def legacy_history(db: Session, actor: User) -> Decimal:
    """Mixed pre-existing history. Returns the expected cash on hand."""
    legacy_cashflow(db, actor, "MAIN_ACCOUNT_TOPUP", "PAID", "5000000")  # +5,000,000
    legacy_cashflow(db, actor, "MAIN_ACCOUNT_TOPUP", "PAID", "1000000")  # +1,000,000
    legacy_cashflow(db, actor, "FEED_AYAM", "PAID", "1200000")  # -1,200,000
    # -300,000: only what was actually paid (the voided payment does not count)
    legacy_cashflow(db, actor, "SALARY", "PARTIALLY_PAID", "800000", ("300000", "-100000"))
    legacy_cashflow(db, actor, "UTILITY", "UNPAID", "300000")  # ignored: not paid yet
    legacy_cashflow(db, actor, "OTHER", "UNPAID", "999999")  # ignored: not paid yet
    db.commit()
    return Decimal("4500000")


def row_count(db: Session) -> int:
    return db.query(OperationalCashflowBalance).count()


def test_backfill_creates_the_row_from_existing_history(db: Session, legacy_history: Decimal) -> None:
    assert row_count(db) == 0

    result = backfill_cash_on_hand(db)

    assert result.previous_amount is None
    assert result.new_amount == legacy_history
    assert result.counted_transactions == 4
    balance = get_balance(db, CASH_ON_HAND)
    assert balance is not None and balance.amount == legacy_history
    assert row_count(db) == 1


def test_backfill_overwrites_a_drifted_balance(db: Session, legacy_history: Decimal) -> None:
    db.add(OperationalCashflowBalance(type=CASH_ON_HAND, amount=Decimal("123")))
    db.commit()

    result = backfill_cash_on_hand(db)

    assert result.previous_amount == Decimal("123")
    assert result.changed
    db.expire_all()
    assert get_balance(db, CASH_ON_HAND).amount == legacy_history
    assert row_count(db) == 1


def test_backfill_is_idempotent(db: Session, legacy_history: Decimal) -> None:
    first = backfill_cash_on_hand(db)
    second = backfill_cash_on_hand(db)

    assert first.new_amount == second.new_amount == legacy_history
    assert second.previous_amount == legacy_history
    assert not second.changed
    assert row_count(db) == 1


def test_dry_run_writes_nothing(db: Session, legacy_history: Decimal) -> None:
    result = backfill_cash_on_hand(db, dry_run=True)

    assert result.dry_run
    assert result.new_amount == legacy_history
    assert row_count(db) == 0, "dry run must not seed the row"

    db.add(OperationalCashflowBalance(type=CASH_ON_HAND, amount=Decimal("123")))
    db.commit()
    backfill_cash_on_hand(db, dry_run=True)
    db.expire_all()
    assert get_balance(db, CASH_ON_HAND).amount == Decimal("123"), "dry run must not overwrite"


def test_empty_history_backfills_zero(db: Session) -> None:
    result = backfill_cash_on_hand(db)

    assert result.new_amount == Decimal("0")
    assert result.counted_transactions == 0
    assert get_balance(db, CASH_ON_HAND).amount == Decimal("0")


def test_backfill_agrees_with_the_live_incremental_balance(db: Session, actor: User) -> None:
    """The SQL recalculation and the per-transaction sync must implement the same rules."""

    def save(cashflow_type: str, amount: str, details: dict[str, object]) -> OperationalCashflow:
        data = {
            "cashflow_type": cashflow_type,
            "transaction_date": today_local(),
            "amount_idr": Decimal(amount),
            "details": details,
        }
        row = save_operational(db, data, actor)
        db.commit()
        return row

    def approve(row: OperationalCashflow) -> None:
        submit_operational(db, row, actor)
        decide_operational(db, row, actor, "APPROVED", None)
        db.commit()

    save("MAIN_ACCOUNT_TOPUP", "2000000", {"notes": "top up"})
    paid = save("UTILITY", "250000", {"description": "electricity"})
    approve(paid)
    pay_operational(db, paid, actor, today_local(), "FULL", None)
    partial = save("OTHER", "400000", {"description": "repairs"})
    approve(partial)
    pay_operational(db, partial, actor, today_local(), "PARTIAL", Decimal("100000"))
    voided = save("UTILITY", "75000", {"description": "water"})
    approve(voided)
    voided = pay_operational(db, voided, actor, today_local(), "FULL", None)
    void_operational_payment(db, voided, voided.payments[0], actor, "wrong entry")
    save("UTILITY", "50000", {"description": "still unpaid"})
    db.commit()

    live = get_balance(db, CASH_ON_HAND)
    assert live is not None
    assert live.amount == Decimal("1650000")  # 2,000,000 - 250,000 - 100,000 (partial payment only)
    recalculated, _ = calculate_cash_on_hand(db)
    assert recalculated == live.amount

    result = backfill_cash_on_hand(db)
    assert not result.changed


def test_cli_main(db: Session, legacy_history: Decimal, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Recalculated cash_on_hand amount:    4500000" in out
    assert "Dry run: nothing was written." in out
    assert row_count(db) == 0

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Previous cash_on_hand amount:        (no row yet)" in out
    assert "cash_on_hand balance updated." in out
    assert get_balance(db, CASH_ON_HAND).amount == legacy_history

    assert main([]) == 0
    assert "already up to date" in capsys.readouterr().out
