"""Checks for the ``approved_unpaid`` row of operational_cashflow_balances.

Rule under test: the balance equals, over every operational cashflow whose ``approval_status`` is
``APPROVED``, the part that is still unpaid: its *latest* ``amount_idr`` minus the sum of its
non-voided payments. Unpaid rows count in full, partially paid rows count their remainder, paid
rows count nothing.

Like the other balance tests these run against the real PostgreSQL test database (see
tests/conftest.py), which is wiped before and after every test.
"""

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.db.models import OperationalCashflow, OperationalCashflowBalance, OperationalCashflowPayment, User
from app.features.cash_balance_service import (
    APPROVED_UNPAID,
    BALANCE_CALCULATORS,
    CASH_ON_HAND,
    WAITING_APPROVAL,
    approved_unpaid_effect,
    calculate_approved_unpaid,
    get_balance,
)
from app.features.common import today_local
from app.features.finance_service import (
    decide_operational,
    pay_operational,
    save_operational,
    submit_operational,
    void_operational_payment,
)
from app.main import app
from app.scripts.backfill_cash_on_hand import backfill_approved_unpaid, backfill_balance, main


def topup(amount: str) -> dict[str, object]:
    return {
        "cashflow_type": "MAIN_ACCOUNT_TOPUP",
        "transaction_date": today_local(),
        "amount_idr": Decimal(amount),
        "details": {"notes": "Top up"},
    }


def utility(amount: str, description: str = "Electricity bill") -> dict[str, object]:
    return {
        "cashflow_type": "UTILITY",
        "transaction_date": today_local(),
        "amount_idr": Decimal(amount),
        "details": {"description": description},
    }


def stored(db: Session, balance_type: str = APPROVED_UNPAID) -> Decimal | None:
    """Current stored amount of a balance, or None when the row was never created."""
    db.expire_all()
    balance = get_balance(db, balance_type)
    return None if balance is None else Decimal(balance.amount)


def assert_matches_recalculation(db: Session) -> None:
    """Every live (incrementally maintained) balance must equal a from-scratch recalculation."""
    for balance_type, calculate in BALANCE_CALCULATORS.items():
        recalculated, _ = calculate(db)
        assert (stored(db, balance_type) or Decimal("0")) == recalculated, balance_type


def create(db: Session, actor: User, data: dict[str, object]) -> OperationalCashflow:
    row = save_operational(db, data, actor)
    db.commit()
    return row


def approved(db: Session, actor: User, amount: str, description: str = "Electricity bill") -> OperationalCashflow:
    """Create an outflow and take it through submit + approve."""
    row = create(db, actor, utility(amount, description))
    submit_operational(db, row, actor)
    db.commit()
    row = decide_operational(db, row, actor, "APPROVED", None)
    db.commit()
    return row


def pay(db: Session, actor: User, row: OperationalCashflow, amount: str | None = None) -> OperationalCashflow:
    mode = "FULL" if amount is None else "PARTIAL"
    row = pay_operational(db, row, actor, today_local(), mode, None if amount is None else Decimal(amount))
    db.commit()
    return row


def edit(db: Session, actor: User, row: OperationalCashflow, amount: str, *, correction: bool) -> OperationalCashflow:
    row = save_operational(
        db, utility(amount), actor, row, correction=correction, reason="Amount fix" if correction else None
    )
    db.commit()
    return row


# --- Pure rule ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("approval_status", "paid", "expected"),
    [
        ("APPROVED", "0", "100"),  # unpaid: full amount
        ("APPROVED", "30", "70"),  # partially paid: remainder
        ("APPROVED", "100", "0"),  # paid: nothing
        ("DRAFT", "0", "0"),
        ("SUBMITTED", "0", "0"),
        ("REJECTED", "0", "0"),
        ("NOT_REQUIRED", "0", "0"),
    ],
)
def test_rule(approval_status: str, paid: str, expected: str) -> None:
    assert approved_unpaid_effect(approval_status, Decimal("100"), Decimal(paid)) == Decimal(expected)


# --- Live updates through the existing operational cashflow transactions -----------------------


def test_example_two_unpaid_and_one_partially_paid(db: Session, actor: User) -> None:
    """2 unpaid rows of 100 each + 1 partially paid row with 50 still unpaid = 250."""
    approved(db, actor, "100", "first")
    approved(db, actor, "100", "second")
    partial = approved(db, actor, "100", "partial")
    pay(db, actor, partial, "50")

    assert partial.payment_status == "PARTIALLY_PAID"
    assert stored(db) == Decimal("250")
    assert calculate_approved_unpaid(db) == (Decimal("250"), 3)


def test_only_approved_cashflows_count(db: Session, actor: User) -> None:
    create(db, actor, topup("1000000"))
    draft = create(db, actor, utility("111"))
    submitted = create(db, actor, utility("222"))
    submit_operational(db, submitted, actor)
    db.commit()
    assert draft.approval_status == "DRAFT"
    assert stored(db) is None, "nothing approved yet, so the row is not even created"

    rejected = create(db, actor, utility("333"))
    submit_operational(db, rejected, actor)
    decide_operational(db, rejected, actor, "REJECTED", "No budget")
    db.commit()
    assert stored(db) is None

    approved(db, actor, "444")
    assert stored(db) == Decimal("444")
    assert stored(db, WAITING_APPROVAL) == Decimal("222")


def test_payments_reduce_and_voids_restore_the_unpaid_remainder(db: Session, actor: User) -> None:
    row = approved(db, actor, "1000")
    assert stored(db) == Decimal("1000")

    row = pay(db, actor, row, "300")
    assert stored(db) == Decimal("700")
    row = pay(db, actor, row, "200")
    assert stored(db) == Decimal("500")

    first_payment = min(row.payments, key=lambda item: item.id)
    row = void_operational_payment(db, row, first_payment, actor, "Recorded in error")
    db.commit()
    assert row.payment_status == "PARTIALLY_PAID"
    assert stored(db) == Decimal("800"), "voiding the 300 payment puts it back as unpaid"

    row = pay(db, actor, row)  # FULL pays the remaining 800
    assert row.payment_status == "PAID"
    assert stored(db) == Decimal("0")
    assert_matches_recalculation(db)


def test_editing_an_approved_unpaid_row_to_a_lower_amount_keeps_it_approved(db: Session, actor: User) -> None:
    approved(db, actor, "500", "other")
    row = approved(db, actor, "1000")
    assert stored(db) == Decimal("1500")

    row = edit(db, actor, row, "800", correction=False)
    assert row.approval_status == "APPROVED"
    assert stored(db) == Decimal("1300")


def test_editing_an_approved_unpaid_row_to_a_higher_amount_sends_it_back_for_approval(db: Session, actor: User) -> None:
    row = approved(db, actor, "1000")

    row = edit(db, actor, row, "1200", correction=False)
    assert row.approval_status == "DRAFT", "existing rule: a higher amount needs a new approval"
    assert stored(db) == Decimal("0")

    submit_operational(db, row, actor)
    db.commit()
    assert stored(db, WAITING_APPROVAL) == Decimal("1200")
    decide_operational(db, row, actor, "APPROVED", None)
    db.commit()
    assert stored(db) == Decimal("1200"), "re-approval counts the latest amount"
    assert stored(db, WAITING_APPROVAL) == Decimal("0")


def test_correcting_a_partially_paid_row_uses_latest_amount_minus_paid(db: Session, actor: User) -> None:
    row = approved(db, actor, "1000")
    row = pay(db, actor, row, "300")
    assert stored(db) == Decimal("700")

    row = edit(db, actor, row, "900", correction=True)
    assert row.approval_status == "APPROVED"
    assert row.payment_status == "PARTIALLY_PAID"
    assert stored(db) == Decimal("600"), "900 (latest amount) - 300 (paid)"

    row = edit(db, actor, row, "300", correction=True)
    assert row.payment_status == "PAID", "the amount now equals what was paid"
    assert stored(db) == Decimal("0")
    assert_matches_recalculation(db)


def test_correcting_a_partially_paid_row_upwards_waits_for_reapproval(db: Session, actor: User) -> None:
    row = approved(db, actor, "1000")
    row = pay(db, actor, row, "300")

    row = edit(db, actor, row, "1500", correction=True)
    assert (row.approval_status, row.payment_status) == ("DRAFT", "PARTIALLY_PAID")
    assert stored(db) == Decimal("0"), "not approved any more, so nothing counts as approved unpaid"

    submit_operational(db, row, actor)
    decide_operational(db, row, actor, "APPROVED", None)
    db.commit()
    assert stored(db) == Decimal("1200"), "1500 (latest amount) - 300 (already paid)"
    assert_matches_recalculation(db)


def test_correcting_a_paid_row_upwards_and_reapproving_counts_only_the_new_remainder(db: Session, actor: User) -> None:
    row = approved(db, actor, "1000")
    row = pay(db, actor, row)
    assert stored(db) == Decimal("0")

    row = edit(db, actor, row, "1250", correction=True)
    assert (row.approval_status, row.payment_status) == ("DRAFT", "PARTIALLY_PAID")
    assert stored(db) == Decimal("0")

    submit_operational(db, row, actor)
    decide_operational(db, row, actor, "APPROVED", None)
    db.commit()
    assert stored(db) == Decimal("250")

    pay(db, actor, row)
    assert stored(db) == Decimal("0")
    assert_matches_recalculation(db)


def test_all_balances_stay_consistent_over_a_mixed_history(db: Session, actor: User) -> None:
    create(db, actor, topup("5000"))
    unpaid = approved(db, actor, "1000", "unpaid")
    partial = pay(db, actor, approved(db, actor, "800", "partial"), "200")
    paid = pay(db, actor, approved(db, actor, "300", "paid"))
    void_operational_payment(db, paid, paid.payments[0], actor, "bounced")
    db.commit()
    edit(db, actor, partial, "700", correction=True)
    edit(db, actor, unpaid, "1100", correction=False)  # back to DRAFT
    waiting = create(db, actor, utility("50", "waiting"))
    submit_operational(db, waiting, actor)
    db.commit()

    # partial: 700 - 200 = 500, paid-then-voided: 300, unpaid went back to draft: 0
    assert stored(db) == Decimal("800")
    assert stored(db, WAITING_APPROVAL) == Decimal("50")
    assert stored(db, CASH_ON_HAND) == Decimal("4800")  # 5000 - 200 (only the partial payment left the cash)
    assert_matches_recalculation(db)


def test_api_flow_updates_approved_unpaid(db: Session, actor: User, actor_password: str) -> None:
    """Same rules, exercised through the existing HTTP endpoints (no new endpoint involved)."""
    base = "/api/v1/operational-cashflows"
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-FM-API-Key": os.environ["API_KEY"]},
            json={"email": actor.email, "password": actor_password},
        )
        assert login.status_code == 200
        headers = {
            "X-FM-API-Key": os.environ["API_KEY"],
            "Authorization": f"Bearer {login.json()['data']['access_token']}",
        }
        transaction = {
            "cashflow_type": "UTILITY",
            "transaction_date": today_local().isoformat(),
            "amount_idr": "1000",
            "details": {"description": "Electricity bill"},
        }

        created = client.post(base, headers=headers, json=transaction)
        assert created.status_code == 201
        cashflow_id = created.json()["data"]["id"]
        assert client.post(f"{base}/{cashflow_id}/submit", headers=headers).status_code == 200
        decision = client.post(f"{base}/{cashflow_id}/decision", headers=headers, json={"decision": "APPROVED"})
        assert decision.status_code == 200
        assert stored(db) == Decimal("1000")

        payment = client.post(
            f"{base}/{cashflow_id}/payments",
            headers=headers,
            json={"payment_mode": "PARTIAL", "paid_date": today_local().isoformat(), "amount_idr": "400"},
        )
        assert payment.status_code == 201
        assert stored(db) == Decimal("600")

        correction = client.post(
            f"{base}/{cashflow_id}/correction",
            headers=headers,
            json={"transaction": transaction | {"amount_idr": "900"}, "correction_reason": "Wrong bill"},
        )
        assert correction.status_code == 200
        assert stored(db) == Decimal("500"), "900 (latest amount) - 400 (paid)"

        payment_id = payment.json()["data"]["payments"][0]["id"]
        voided = client.patch(f"{base}/{cashflow_id}/payments/{payment_id}/void", headers=headers, json={"reason": "x"})
        assert voided.status_code == 200
        assert stored(db) == Decimal("900")


# --- Schema -----------------------------------------------------------------------------------


def test_balance_type_constraint_allows_approved_unpaid(db: Session) -> None:
    db.execute(insert(OperationalCashflowBalance).values(type=APPROVED_UNPAID, amount=Decimal("0")))
    db.commit()
    assert stored(db) == Decimal("0")


# --- Backfill for cashflows that were already approved before this balance existed ------------


def legacy_cashflow(
    db: Session, actor: User, approval_status: str, amount: str, payments: tuple[str, ...] = ()
) -> None:
    """Insert straight into operational_cashflows, bypassing the service (and so the live sync).
    A payment prefixed with ``-`` is stored as voided."""
    paid = sum((Decimal(value) for value in payments if not value.startswith("-")), Decimal("0"))
    status = "UNPAID" if paid == 0 else ("PAID" if paid == Decimal(amount) else "PARTIALLY_PAID")
    row = OperationalCashflow(
        cashflow_type="OTHER",
        direction="OUTFLOW",
        transaction_date=date(2026, 1, 15),
        amount_idr=Decimal(amount),
        approval_status=approval_status,
        payment_status=status,
        created_by=actor.id,
    )
    db.add(row)
    db.flush()
    for value in payments:
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
def legacy_approved(db: Session, actor: User) -> Decimal:
    """Pre-existing history. Returns the expected approved_unpaid amount."""
    legacy_cashflow(db, actor, "APPROVED", "100")  # 100
    legacy_cashflow(db, actor, "APPROVED", "100")  # 100
    legacy_cashflow(db, actor, "APPROVED", "100", ("50",))  # 50
    legacy_cashflow(db, actor, "APPROVED", "400", ("100", "-150"))  # 300: voided payment ignored
    legacy_cashflow(db, actor, "APPROVED", "500", ("500",))  # 0: fully paid
    legacy_cashflow(db, actor, "SUBMITTED", "999")  # 0: not approved
    legacy_cashflow(db, actor, "DRAFT", "888")  # 0: not approved
    db.commit()
    return Decimal("550")


def test_backfill_seeds_existing_approved_cashflows(db: Session, legacy_approved: Decimal) -> None:
    dry = backfill_approved_unpaid(db, dry_run=True)
    assert dry.new_amount == legacy_approved
    assert stored(db) is None, "dry run writes nothing"

    result = backfill_approved_unpaid(db)
    assert result.previous_amount is None
    assert result.new_amount == legacy_approved
    assert result.counted_transactions == 4
    assert stored(db) == legacy_approved
    assert not backfill_approved_unpaid(db).changed, "backfill is idempotent"


def test_after_backfill_paying_a_legacy_row_keeps_the_balance_exact(
    db: Session, actor: User, legacy_approved: Decimal
) -> None:
    for balance_type in BALANCE_CALCULATORS:
        backfill_balance(db, balance_type)
    legacy = db.query(OperationalCashflow).filter_by(approval_status="APPROVED", amount_idr=400).one()

    pay(db, actor, legacy)  # pays the remaining 300
    assert stored(db) == legacy_approved - Decimal("300")
    assert_matches_recalculation(db)


def test_cli_backfills_approved_unpaid(
    db: Session, legacy_approved: Decimal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--type", "approved_unpaid"]) == 0
    out = capsys.readouterr().out
    assert "Approved transactions not fully paid: 4" in out
    assert "Recalculated approved_unpaid amount:  550" in out
    assert "approved_unpaid balance updated." in out
    assert "waiting_approval" not in out and "cash_on_hand" not in out
    assert stored(db) == legacy_approved

    assert main([]) == 0
    assert "approved_unpaid balance already up to date; nothing to change." in capsys.readouterr().out
