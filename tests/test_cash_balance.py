"""End-to-end checks for the operational_cashflow_balances ledger (cash_on_hand).

These tests talk to the real PostgreSQL test database (see tests/conftest.py) because the
balance logic relies on row locking and on the actual operational_cashflows constraints.
Every test cleans up the rows it creates so the suite can run repeatedly against the same
database.
"""

import os
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import OperationalCashflowBalance, User
from app.features.cash_balance_service import CASH_ON_HAND, calculate_cash_on_hand, cash_on_hand, get_balance
from app.features.common import today_local
from app.features.finance_service import (
    decide_operational,
    pay_operational,
    save_operational,
    submit_operational,
    void_operational_payment,
)
from app.main import app


def topup(amount: Decimal) -> dict[str, object]:
    return {
        "cashflow_type": "MAIN_ACCOUNT_TOPUP",
        "transaction_date": today_local(),
        "amount_idr": amount,
        "details": {"notes": "Top up"},
    }


def utility(amount: Decimal, description: str = "Electricity bill") -> dict[str, object]:
    return {
        "cashflow_type": "UTILITY",
        "transaction_date": today_local(),
        "amount_idr": amount,
        "details": {"description": description},
    }


def test_balance_row_is_seeded_with_zero_when_table_is_empty(db: Session) -> None:
    assert db.query(OperationalCashflowBalance).count() == 0
    balance = cash_on_hand(db)
    assert balance.type == CASH_ON_HAND
    assert balance.amount == Decimal("0")
    # Still only one row, and fetching again reuses it instead of inserting another.
    assert db.query(OperationalCashflowBalance).count() == 1
    again = cash_on_hand(db)
    assert again.id == balance.id


def test_topup_adds_directly_to_cash_on_hand(db: Session, actor: User) -> None:
    save_operational(db, topup(Decimal("500000")), actor)
    db.commit()
    balance = get_balance(db, CASH_ON_HAND)
    assert balance is not None
    assert balance.amount == Decimal("500000")
    first_update = balance.updated_at

    # A second top-up keeps adding on top of the existing balance and bumps updated_at.
    save_operational(db, topup(Decimal("250000")), actor)
    db.commit()
    balance = get_balance(db, CASH_ON_HAND)
    assert balance.amount == Decimal("750000")
    db.refresh(balance)
    assert balance.updated_at > first_update
    assert db.query(OperationalCashflowBalance).count() == 1


def test_unpaid_outflow_does_not_affect_balance_until_paid(db: Session, actor: User) -> None:
    save_operational(db, topup(Decimal("1000000")), actor)
    db.commit()

    row = save_operational(db, utility(Decimal("120000")), actor)
    db.commit()
    assert row.payment_status == "UNPAID"
    balance = get_balance(db, CASH_ON_HAND)
    assert balance.amount == Decimal("1000000")

    submit_operational(db, row, actor)
    db.commit()
    decide_operational(db, row, actor, "APPROVED", None)
    db.commit()
    balance = get_balance(db, CASH_ON_HAND)
    assert balance.amount == Decimal("1000000"), "approval alone must not move cash on hand"

    pay_operational(db, row, actor, today_local(), "FULL", None)
    db.commit()
    assert row.payment_status == "PAID"
    balance = get_balance(db, CASH_ON_HAND)
    assert balance.amount == Decimal("880000")


def test_partial_payment_deducts_only_the_paid_amount_and_full_payment_deducts_the_rest(
    db: Session, actor: User
) -> None:
    save_operational(db, topup(Decimal("1000000")), actor)
    db.commit()

    row = save_operational(db, utility(Decimal("200000")), actor)
    db.commit()
    submit_operational(db, row, actor)
    db.commit()
    decide_operational(db, row, actor, "APPROVED", None)
    db.commit()

    pay_operational(db, row, actor, today_local(), "PARTIAL", Decimal("50000"))
    db.commit()
    assert row.payment_status == "PARTIALLY_PAID"
    balance = get_balance(db, CASH_ON_HAND)
    assert balance.amount == Decimal("950000"), "only the partially paid amount leaves the cash on hand"

    pay_operational(db, row, actor, today_local(), "FULL", None)
    db.commit()
    assert row.payment_status == "PAID"
    balance = get_balance(db, CASH_ON_HAND)
    assert balance.amount == Decimal("800000"), "the settling payment deducts only the remaining 150,000"


def test_voiding_the_only_payment_restores_the_balance(db: Session, actor: User) -> None:
    save_operational(db, topup(Decimal("1000000")), actor)
    db.commit()

    row = save_operational(db, utility(Decimal("150000")), actor)
    db.commit()
    submit_operational(db, row, actor)
    db.commit()
    decide_operational(db, row, actor, "APPROVED", None)
    db.commit()
    row = pay_operational(db, row, actor, today_local(), "FULL", None)
    db.commit()
    balance = get_balance(db, CASH_ON_HAND)
    assert balance.amount == Decimal("850000")

    payment = row.payments[0]
    void_operational_payment(db, row, payment, actor, "Recorded in error")
    db.commit()
    assert row.payment_status == "UNPAID"
    balance = get_balance(db, CASH_ON_HAND)
    assert balance.amount == Decimal("1000000")


def test_each_partial_payment_voids_and_corrections_move_only_the_paid_amount(db: Session, actor: User) -> None:
    save_operational(db, topup(Decimal("1000000")), actor)
    db.commit()
    row = save_operational(db, utility(Decimal("300000")), actor)
    submit_operational(db, row, actor)
    decide_operational(db, row, actor, "APPROVED", None)
    db.commit()

    def amount() -> Decimal:
        db.expire_all()
        return get_balance(db, CASH_ON_HAND).amount

    row = pay_operational(db, row, actor, today_local(), "PARTIAL", Decimal("100000"))
    db.commit()
    assert amount() == Decimal("900000")
    row = pay_operational(db, row, actor, today_local(), "PARTIAL", Decimal("50000"))
    db.commit()
    assert amount() == Decimal("850000"), "each partial payment subtracts its own amount"

    row = void_operational_payment(db, row, row.payments[0], actor, "Recorded twice")
    db.commit()
    assert row.payment_status == "PARTIALLY_PAID"
    assert amount() == Decimal("950000"), "the voided 100,000 comes back"

    # A correction of the amount does not move money: only the 50,000 paid has left.
    row = save_operational(db, utility(Decimal("250000")), actor, row, correction=True, reason="Discount")
    db.commit()
    assert amount() == Decimal("950000")

    pay_operational(db, row, actor, today_local(), "FULL", None)
    db.commit()
    assert amount() == Decimal("750000"), "the settling payment subtracts the remaining 200,000"
    recalculated, _ = calculate_cash_on_hand(db)
    assert recalculated == Decimal("750000"), "the backfill recalculation agrees with the live balance"


def test_cash_on_hand_endpoint_returns_the_stored_amount(db: Session, actor: User, actor_password: str) -> None:
    save_operational(db, topup(Decimal("42000")), actor)
    db.commit()

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-FM-API-Key": os.environ["API_KEY"]},
            json={"email": actor.email, "password": actor_password},
        )
        assert login.status_code == 200
        token = login.json()["data"]["access_token"]

        response = client.get(
            "/api/v1/operational-cashflows/cash-on-hand",
            headers={
                "X-FM-API-Key": os.environ["API_KEY"],
                "Authorization": f"Bearer {token}",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body["data"]) == {"amount", "updated_at"}, "only amount and latest update are exposed"
    assert body["data"]["amount"] == "42000"
    stored = get_balance(db, CASH_ON_HAND)
    assert stored is not None
    db.refresh(stored)
    assert datetime.fromisoformat(body["data"]["updated_at"]) == stored.updated_at
