"""Checks for the ``waiting_approval`` row of operational_cashflow_balances.

Rule under test: the balance equals the total ``amount_idr`` of operational cashflows whose
``approval_status`` is ``SUBMITTED``. A row entering ``SUBMITTED`` (new or existing) adds its
amount; leaving it through approval or rejection removes the amount again.

Like tests/test_cash_balance.py these run against the real PostgreSQL test database (see
tests/conftest.py), which is wiped before and after every test.
"""

import os
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import OperationalCashflow, OperationalCashflowBalance, User
from app.features.cash_balance_service import (
    CASH_ON_HAND,
    WAITING_APPROVAL,
    CashflowState,
    calculate_waiting_approval,
    get_balance,
    sync_balances,
    waiting_approval_effect,
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
from app.scripts.backfill_cash_on_hand import backfill_waiting_approval, main


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


def waiting(db: Session) -> Decimal | None:
    """Current stored waiting_approval amount, or None when the row was never created."""
    db.expire_all()
    balance = get_balance(db, WAITING_APPROVAL)
    return None if balance is None else Decimal(balance.amount)


def cash(db: Session) -> Decimal | None:
    db.expire_all()
    balance = get_balance(db, CASH_ON_HAND)
    return None if balance is None else Decimal(balance.amount)


def create(db: Session, actor: User, data: dict[str, object]) -> OperationalCashflow:
    row = save_operational(db, data, actor)
    db.commit()
    return row


def submit(db: Session, actor: User, row: OperationalCashflow) -> OperationalCashflow:
    row = submit_operational(db, row, actor)
    db.commit()
    return row


def approve(db: Session, actor: User, row: OperationalCashflow) -> OperationalCashflow:
    row = decide_operational(db, row, actor, "APPROVED", None)
    db.commit()
    return row


def reject(db: Session, actor: User, row: OperationalCashflow, reason: str = "Wrong amount") -> OperationalCashflow:
    row = decide_operational(db, row, actor, "REJECTED", reason)
    db.commit()
    return row


# --- Pure rule ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("approval_status", "expected"),
    [
        ("SUBMITTED", Decimal("125000")),
        ("DRAFT", Decimal("0")),
        ("APPROVED", Decimal("0")),
        ("REJECTED", Decimal("0")),
        ("NOT_REQUIRED", Decimal("0")),
    ],
)
def test_only_submitted_cashflows_count_as_waiting_approval(approval_status: str, expected: Decimal) -> None:
    assert waiting_approval_effect(approval_status, Decimal("125000")) == expected


# --- Live updates through the existing operational cashflow transactions -----------------------


def test_new_draft_or_topup_does_not_add_to_waiting_approval(db: Session, actor: User) -> None:
    create(db, actor, topup("1000000"))
    draft = create(db, actor, utility("300000"))

    assert draft.approval_status == "DRAFT"
    assert waiting(db) is None, "nothing is waiting for approval yet, so the row is not even created"


def test_submitting_adds_the_amount(db: Session, actor: User) -> None:
    first = create(db, actor, utility("300000"))
    second = create(db, actor, utility("50000", "Water bill"))

    submit(db, actor, first)
    assert waiting(db) == Decimal("300000")

    submit(db, actor, second)
    assert waiting(db) == Decimal("350000")
    assert db.query(OperationalCashflowBalance).filter_by(type=WAITING_APPROVAL).count() == 1


def test_approving_removes_the_amount(db: Session, actor: User) -> None:
    first = submit(db, actor, create(db, actor, utility("300000")))
    second = submit(db, actor, create(db, actor, utility("50000", "Water bill")))
    assert waiting(db) == Decimal("350000")

    approve(db, actor, first)
    assert waiting(db) == Decimal("50000")

    approve(db, actor, second)
    assert waiting(db) == Decimal("0")


def test_rejecting_removes_the_amount(db: Session, actor: User) -> None:
    row = submit(db, actor, create(db, actor, utility("300000")))
    assert waiting(db) == Decimal("300000")

    reject(db, actor, row)
    assert row.approval_status == "REJECTED"
    assert waiting(db) == Decimal("0")


def test_rejected_then_edited_and_resubmitted_is_counted_again(db: Session, actor: User) -> None:
    row = submit(db, actor, create(db, actor, utility("300000")))
    reject(db, actor, row)

    row = save_operational(db, utility("280000"), actor, row)
    db.commit()
    assert waiting(db) == Decimal("0"), "editing a rejected cashflow does not submit it"

    submit(db, actor, row)
    assert waiting(db) == Decimal("280000"), "the resubmitted (edited) amount is added"

    approve(db, actor, row)
    assert waiting(db) == Decimal("0")


def test_correcting_a_submitted_cashflow_adjusts_by_the_difference(db: Session, actor: User) -> None:
    other = submit(db, actor, create(db, actor, utility("100000", "Water bill")))
    row = submit(db, actor, create(db, actor, utility("300000")))
    assert waiting(db) == Decimal("400000")

    row = save_operational(db, utility("250000"), actor, row, correction=True, reason="Typo in amount")
    db.commit()
    assert row.approval_status == "SUBMITTED"
    assert waiting(db) == Decimal("350000")

    # Approval removes the corrected amount, not the originally submitted one.
    approve(db, actor, row)
    assert waiting(db) == Decimal("100000")
    reject(db, actor, other)
    assert waiting(db) == Decimal("0")


def test_invalid_transitions_leave_the_balance_untouched(db: Session, actor: User) -> None:
    draft = create(db, actor, utility("70000", "Still a draft"))
    row = submit(db, actor, create(db, actor, utility("300000")))

    with pytest.raises(AppError):
        submit_operational(db, row, actor)  # already submitted
    db.rollback()
    with pytest.raises(AppError):
        decide_operational(db, draft, actor, "APPROVED", None)  # never submitted
    db.rollback()
    assert waiting(db) == Decimal("300000")

    approve(db, actor, row)
    with pytest.raises(AppError):
        decide_operational(db, row, actor, "REJECTED", "too late")  # already decided
    db.rollback()
    assert waiting(db) == Decimal("0")


def test_payments_do_not_touch_waiting_approval_and_approval_does_not_touch_cash(db: Session, actor: User) -> None:
    create(db, actor, topup("1000000"))
    row = submit(db, actor, create(db, actor, utility("200000")))
    assert cash(db) == Decimal("1000000"), "submitting must not move cash on hand"
    assert waiting(db) == Decimal("200000")

    approve(db, actor, row)
    assert cash(db) == Decimal("1000000"), "approving must not move cash on hand"
    assert waiting(db) == Decimal("0")

    row = pay_operational(db, row, actor, today_local(), "FULL", None)
    db.commit()
    assert cash(db) == Decimal("800000")
    assert waiting(db) == Decimal("0")

    void_operational_payment(db, row, row.payments[0], actor, "Recorded in error")
    db.commit()
    assert cash(db) == Decimal("1000000")
    assert waiting(db) == Decimal("0")


def test_a_row_that_is_new_and_already_submitted_is_added(db: Session, actor: User) -> None:
    """The service always creates outflows as DRAFT today, but the sync treats a brand new row
    (no ``before`` snapshot) in SUBMITTED state as entering the waiting queue."""
    row = OperationalCashflow(
        cashflow_type="UTILITY",
        direction="OUTFLOW",
        transaction_date=today_local(),
        amount_idr=Decimal("90000"),
        approval_status="SUBMITTED",
        payment_status="UNPAID",
        created_by=actor.id,
    )
    db.add(row)
    db.flush()
    sync_balances(db, None, row)
    db.commit()
    assert waiting(db) == Decimal("90000")

    before = CashflowState.of(row)
    row.approval_status = "APPROVED"
    db.flush()
    sync_balances(db, before, row)
    db.commit()
    assert waiting(db) == Decimal("0")


def test_api_flow_updates_waiting_approval(db: Session, actor: User, actor_password: str) -> None:
    """Same rules, exercised through the existing HTTP endpoints (no new endpoint involved)."""
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
        payload = {
            "cashflow_type": "UTILITY",
            "transaction_date": today_local().isoformat(),
            "amount_idr": "450000",
            "details": {"description": "Electricity bill"},
        }

        first = client.post("/api/v1/operational-cashflows", headers=headers, json=payload)
        second = client.post("/api/v1/operational-cashflows", headers=headers, json=payload | {"amount_idr": "50000"})
        assert first.status_code == second.status_code == 201
        first_id, second_id = first.json()["data"]["id"], second.json()["data"]["id"]
        assert waiting(db) is None

        for cashflow_id in (first_id, second_id):
            response = client.post(f"/api/v1/operational-cashflows/{cashflow_id}/submit", headers=headers)
            assert response.status_code == 200
        assert waiting(db) == Decimal("500000")

        response = client.post(
            f"/api/v1/operational-cashflows/{first_id}/decision",
            headers=headers,
            json={"decision": "APPROVED"},
        )
        assert response.status_code == 200
        assert waiting(db) == Decimal("50000")

        response = client.post(
            f"/api/v1/operational-cashflows/{second_id}/decision",
            headers=headers,
            json={"decision": "REJECTED", "reason": "Duplicate"},
        )
        assert response.status_code == 200
        assert waiting(db) == Decimal("0")


# --- Schema -----------------------------------------------------------------------------------


def test_balance_type_constraint_allows_waiting_approval_and_rejects_unknown_types(db: Session) -> None:
    db.execute(insert(OperationalCashflowBalance).values(type=WAITING_APPROVAL, amount=Decimal("0")))
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(insert(OperationalCashflowBalance).values(type="not_a_balance", amount=Decimal("0")))
    db.rollback()


# --- Backfill for cashflows that were already submitted before this balance existed ---------


def legacy_cashflow(db: Session, actor: User, approval_status: str, amount: str) -> None:
    """Insert straight into operational_cashflows, bypassing the service (and so the live sync)."""
    db.add(
        OperationalCashflow(
            cashflow_type="OTHER",
            direction="OUTFLOW",
            transaction_date=date(2026, 1, 15),
            amount_idr=Decimal(amount),
            approval_status=approval_status,
            payment_status="UNPAID",
            created_by=actor.id,
        )
    )


@pytest.fixture
def legacy_submitted(db: Session, actor: User) -> Decimal:
    """Pre-existing history. Returns the expected waiting_approval amount."""
    legacy_cashflow(db, actor, "SUBMITTED", "400000")
    legacy_cashflow(db, actor, "SUBMITTED", "150000")
    legacy_cashflow(db, actor, "DRAFT", "999999")
    legacy_cashflow(db, actor, "APPROVED", "888888")
    legacy_cashflow(db, actor, "REJECTED", "777777")
    db.commit()
    return Decimal("550000")


def test_backfill_seeds_existing_submitted_cashflows(db: Session, legacy_submitted: Decimal) -> None:
    dry = backfill_waiting_approval(db, dry_run=True)
    assert dry.new_amount == legacy_submitted
    assert waiting(db) is None, "dry run writes nothing"

    result = backfill_waiting_approval(db)
    assert result.previous_amount is None
    assert result.new_amount == legacy_submitted
    assert result.counted_transactions == 2
    assert waiting(db) == legacy_submitted

    again = backfill_waiting_approval(db)
    assert not again.changed, "backfill is idempotent"


def test_after_backfill_approving_a_legacy_row_does_not_go_negative(
    db: Session, actor: User, legacy_submitted: Decimal
) -> None:
    backfill_waiting_approval(db)
    legacy = db.query(OperationalCashflow).filter_by(approval_status="SUBMITTED", amount_idr=400000).one()

    approve(db, actor, legacy)
    assert waiting(db) == legacy_submitted - Decimal("400000")


def test_backfill_agrees_with_the_live_incremental_balance(db: Session, actor: User) -> None:
    approved = submit(db, actor, create(db, actor, utility("100000", "approved")))
    approve(db, actor, approved)
    rejected = submit(db, actor, create(db, actor, utility("200000", "rejected")))
    reject(db, actor, rejected)
    corrected = submit(db, actor, create(db, actor, utility("300000", "corrected")))
    save_operational(db, utility("330000", "corrected"), actor, corrected, correction=True, reason="fix")
    db.commit()
    submit(db, actor, create(db, actor, utility("40000", "still waiting")))
    create(db, actor, utility("999999", "draft"))

    live = waiting(db)
    assert live == Decimal("370000")  # 330,000 + 40,000
    assert calculate_waiting_approval(db) == (live, 2)
    assert not backfill_waiting_approval(db).changed


def test_cli_backfills_waiting_approval(
    db: Session, legacy_submitted: Decimal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--type", "waiting_approval"]) == 0
    out = capsys.readouterr().out
    assert "Transactions waiting for approval:    2" in out
    assert "Recalculated waiting_approval amount: 550000" in out
    assert "waiting_approval balance updated." in out
    assert "cash_on_hand" not in out, "--type restricts the run to one balance"
    assert waiting(db) == legacy_submitted
    assert cash(db) is None

    # Without --type every balance is recalculated; waiting_approval is already correct.
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "cash_on_hand balance updated." in out
    assert "waiting_approval balance already up to date; nothing to change." in out
