"""Checks for the main cash account (main_cash_accounts + main_cash_account_logs).

Rules under test:

- a paid ``MAIN_ACCOUNT_TOPUP`` operational cashflow is a ``debit`` of its ``amount_idr``;
- a paid (approved) ``MAIN_ACCOUNT_TRANSFER`` revenue is a ``credit`` of its ``amount_idr``
  (gross minus transfer cost);
- every later change to such a row posts only the difference, so the signed sum of the log always
  equals ``current_amount``;
- manual adjustments go through the API and are tagged ``manual:<note>``.

Like the other balance tests these run against the real PostgreSQL test database (see
tests/conftest.py), which is wiped before and after every test.
"""

import os
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import (
    AuditEvent,
    MainCashAccount,
    MainCashAccountLog,
    OperationalCashflow,
    Revenue,
    RevenueTransferDetail,
    User,
)
from app.features.cash_balance_service import CASH_ON_HAND, get_balance
from app.features.common import today_local
from app.features.finance_service import (
    decide_operational,
    decide_revenue,
    pay_operational,
    pay_revenue,
    save_operational,
    save_revenue,
    submit_operational,
    submit_revenue,
)
from app.features.main_cash_account_events import drain
from app.features.main_cash_account_service import (
    adjust,
    get_account,
    log_total,
    parse_reference,
    topup_effect,
    transfer_effect,
)
from app.main import app
from app.scripts.backfill_main_cash_account import backfill_main_cash_account, main

# --- Helpers ------------------------------------------------------------------------------------
# Posting to the main account is asynchronous (see tests/test_main_cash_account_events.py). These
# tests check the resulting ledger, so the helpers observe it after the background worker caught up.


def topup_data(amount: str) -> dict[str, object]:
    return {
        "cashflow_type": "MAIN_ACCOUNT_TOPUP",
        "transaction_date": today_local(),
        "amount_idr": Decimal(amount),
        "details": {"notes": "Top up"},
    }


def transfer_data(gross: str, cost: str = "0") -> dict[str, object]:
    return {
        "revenue_type": "MAIN_ACCOUNT_TRANSFER",
        "transaction_date": today_local(),
        "details": {"gross_amount_idr": Decimal(gross), "payment_cost_idr": Decimal(cost), "notes": None},
    }


def topup(db: Session, actor: User, amount: str) -> OperationalCashflow:
    row = save_operational(db, topup_data(amount), actor)
    db.commit()
    drain()
    return row


def correct_topup(db: Session, actor: User, row: OperationalCashflow, amount: str) -> OperationalCashflow:
    row = save_operational(db, topup_data(amount), actor, row, correction=True, reason="Wrong amount")
    db.commit()
    drain()
    return row


def transfer(db: Session, actor: User, gross: str, cost: str = "0") -> Revenue:
    row = save_revenue(db, transfer_data(gross, cost), actor)
    db.commit()
    return row


def approve_transfer(db: Session, actor: User, row: Revenue) -> Revenue:
    submit_revenue(db, row, actor)
    db.commit()
    # acknowledge=True: the tests do not seed the revenue account, only the main account matters here.
    row = decide_revenue(db, row, actor, "APPROVED", None, True)
    db.commit()
    drain()
    return row


def amount(db: Session) -> Decimal | None:
    drain()
    db.expire_all()
    account = get_account(db)
    return None if account is None else Decimal(account.current_amount)


def logs(db: Session) -> list[tuple[str, Decimal, str]]:
    """(direction, amount, reference) in posting order."""
    drain()
    db.expire_all()
    rows = db.scalars(select(MainCashAccountLog).order_by(MainCashAccountLog.id)).all()
    return [(row.direction, Decimal(row.amount), row.reference) for row in rows]


def assert_log_matches_amount(db: Session) -> None:
    assert (amount(db) or Decimal("0")) == log_total(db)


# --- Pure rules -----------------------------------------------------------------------------------


def test_rules() -> None:
    assert topup_effect("MAIN_ACCOUNT_TOPUP", "PAID", Decimal("100")) == Decimal("-100")
    assert topup_effect("MAIN_ACCOUNT_TOPUP", "UNPAID", Decimal("100")) == 0
    assert topup_effect("UTILITY", "PAID", Decimal("100")) == 0
    assert transfer_effect("MAIN_ACCOUNT_TRANSFER", "PAID", Decimal("100")) == Decimal("100")
    assert transfer_effect("MAIN_ACCOUNT_TRANSFER", "UNPAID", Decimal("100")) == 0
    assert transfer_effect("OTHER", "PAID", Decimal("100")) == 0


def test_parse_reference() -> None:
    assert parse_reference("operational_cashflows:12:cashflow_created") == {
        "type": "operational_cashflows",
        "id": 12,
        "action": "cashflow_created",
    }
    assert parse_reference("revenues:7:transfer_approved")["type"] == "revenues"
    assert parse_reference("manual:Opening: bank statement") == {"type": "manual", "id": None, "action": None}


# --- Operational cashflow top-ups (debit) --------------------------------------------------------


def test_topup_is_a_debit(db: Session, actor: User) -> None:
    row = topup(db, actor, "1000")

    assert logs(db) == [("debit", Decimal("1000"), f"operational_cashflows:{row.id}:cashflow_created")]
    assert amount(db) == Decimal("-1000")
    assert get_balance(db, CASH_ON_HAND).amount == Decimal("1000"), "the same top-up feeds cash on hand"


def test_topup_correction_posts_only_the_difference(db: Session, actor: User) -> None:
    row = topup(db, actor, "1000")

    row = correct_topup(db, actor, row, "800")
    row = correct_topup(db, actor, row, "1500")
    correct_topup(db, actor, row, "1500")  # no amount change, nothing posted

    reference = f"operational_cashflows:{row.id}:cashflow_corrected"
    assert logs(db)[1:] == [("credit", Decimal("200"), reference), ("debit", Decimal("700"), reference)]
    assert amount(db) == Decimal("-1500")
    assert_log_matches_amount(db)


def test_other_cashflow_types_never_touch_the_main_account(db: Session, actor: User) -> None:
    row = save_operational(
        db,
        {
            "cashflow_type": "UTILITY",
            "transaction_date": today_local(),
            "amount_idr": Decimal("300"),
            "details": {"description": "Electricity"},
        },
        actor,
    )
    db.commit()
    submit_operational(db, row, actor)
    decide_operational(db, row, actor, "APPROVED", None)
    pay_operational(db, row, actor, today_local(), "FULL", None)
    db.commit()

    assert logs(db) == []
    assert amount(db) is None


# --- Revenue transfers (credit) -----------------------------------------------------------------


def test_transfer_is_credited_only_when_approved(db: Session, actor: User) -> None:
    row = transfer(db, actor, "1000", "25")
    assert logs(db) == [], "a draft transfer has not moved any money"

    submit_revenue(db, row, actor)
    db.commit()
    decide_revenue(db, row, actor, "REJECTED", "Wrong account", False)
    db.commit()
    assert logs(db) == [], "submitted and rejected transfers have not moved any money"

    row = save_revenue(db, transfer_data("1200", "25"), actor, row)
    db.commit()
    row = approve_transfer(db, actor, row)

    assert row.payment_status == "PAID"
    assert logs(db) == [("credit", Decimal("1175"), f"revenues:{row.id}:transfer_approved")], "net of transfer cost"
    assert amount(db) == Decimal("1175")


def test_transfer_correction_posts_only_the_difference(db: Session, actor: User) -> None:
    row = approve_transfer(db, actor, transfer(db, actor, "1000"))

    row = save_revenue(db, transfer_data("1000", "100"), actor, row, correction=True, reason="Fee missing")
    db.commit()
    row = save_revenue(db, transfer_data("1300", "100"), actor, row, correction=True, reason="Wrong gross")
    db.commit()

    reference = f"revenues:{row.id}:revenue_corrected"
    assert logs(db)[1:] == [("debit", Decimal("100"), reference), ("credit", Decimal("300"), reference)]
    assert amount(db) == Decimal("1200")
    assert_log_matches_amount(db)


def test_other_revenue_types_never_touch_the_main_account(db: Session, actor: User) -> None:
    row = save_revenue(
        db,
        {
            "revenue_type": "OTHER",
            "transaction_date": today_local(),
            "amount_idr": Decimal("500"),
            "payment_method": "CASH",
            "details": {"notes": "Sold sacks"},
        },
        actor,
    )
    db.commit()
    pay_revenue(db, row, actor, today_local(), "FULL", None)
    db.commit()

    assert logs(db) == []


def test_payment_endpoints_cannot_move_topups_or_transfers(db: Session, actor: User) -> None:
    """The sync hooks live in save/decide only; these guards are why that is complete."""
    top = topup(db, actor, "100")
    with pytest.raises(AppError):
        pay_operational(db, top, actor, today_local(), "FULL", None)
    db.rollback()
    with pytest.raises(AppError):
        submit_operational(db, top, actor)
    db.rollback()
    moved = approve_transfer(db, actor, transfer(db, actor, "100"))
    with pytest.raises(AppError):
        pay_revenue(db, moved, actor, today_local(), "FULL", None)
    db.rollback()

    assert amount(db) == Decimal("0")
    assert len(logs(db)) == 2


def test_log_always_sums_to_current_amount_over_a_mixed_history(db: Session, actor: User) -> None:
    first = topup(db, actor, "1000")
    topup(db, actor, "400")
    moved = approve_transfer(db, actor, transfer(db, actor, "5000", "50"))
    correct_topup(db, actor, first, "900")
    save_revenue(db, transfer_data("4000", "50"), actor, moved, correction=True, reason="fix")
    db.commit()
    adjust(db, "credit", Decimal("250"), "Bank interest", actor)
    db.commit()

    assert amount(db) == Decimal("-900") - Decimal("400") + Decimal("3950") + Decimal("250")
    assert_log_matches_amount(db)
    assert db.query(MainCashAccount).count() == 1


# --- Schema -------------------------------------------------------------------------------------


def test_only_one_main_cash_account_can_exist(db: Session) -> None:
    db.execute(insert(MainCashAccount).values(current_amount=Decimal("0")))
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(insert(MainCashAccount).values(current_amount=Decimal("5")))
    db.rollback()


@pytest.mark.parametrize(("direction", "value"), [("sideways", "10"), ("credit", "0"), ("debit", "-5")])
def test_log_constraints(db: Session, direction: str, value: str) -> None:
    account_id = db.scalar(insert(MainCashAccount).values(current_amount=0).returning(MainCashAccount.id))
    with pytest.raises(IntegrityError):
        db.execute(
            insert(MainCashAccountLog).values(
                main_cash_account_id=account_id, amount=Decimal(value), direction=direction, reference="manual:x"
            )
        )
    db.rollback()


# --- API ----------------------------------------------------------------------------------------


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


def test_api_get_account_seeds_a_zero_account_once(db: Session, client: TestClient) -> None:
    first = client.get("/api/v1/main-cash-account")
    second = client.get("/api/v1/main-cash-account")

    assert first.status_code == 200
    assert set(first.json()["data"]) == {"id", "current_amount", "updated_at", "pending_entries", "pending_amount"}
    assert first.json()["data"]["current_amount"] == "0"
    assert (first.json()["data"]["pending_entries"], first.json()["data"]["pending_amount"]) == (0, "0")
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert db.query(MainCashAccount).count() == 1


def test_api_manual_adjustments(db: Session, actor: User, client: TestClient) -> None:
    credit = client.post(
        "/api/v1/main-cash-account/adjustments",
        json={"direction": "credit", "amount": "10000", "note": "Opening balance from bank statement"},
    )
    assert credit.status_code == 201
    body = credit.json()["data"]
    assert body["account"]["current_amount"] == "10000"
    assert body["log"]["direction"] == "credit"
    assert body["log"]["amount"] == "10000"
    assert body["log"]["reference"] == "manual:Opening balance from bank statement"
    assert body["log"]["source"] == {"type": "manual", "id": None, "action": None}

    debit = client.post(
        "/api/v1/main-cash-account/adjustments",
        json={"direction": "debit", "amount": "1500", "note": "Monthly bank fee"},
    )
    assert debit.status_code == 201
    assert debit.json()["data"]["account"]["current_amount"] == "8500"
    assert_log_matches_amount(db)

    audited = db.scalars(
        select(AuditEvent).where(AuditEvent.entity_type == "main_cash_account_logs").order_by(AuditEvent.id)
    ).all()
    assert [(event.action, event.actor_id, event.reason) for event in audited] == [
        ("main_cash_account_adjusted", actor.id, "Opening balance from bank statement"),
        ("main_cash_account_adjusted", actor.id, "Monthly bank fee"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"direction": "credit", "amount": "0", "note": "zero"},
        {"direction": "credit", "amount": "-5", "note": "negative"},
        {"direction": "credit", "amount": "0.4", "note": "rounds to zero IDR"},
        {"direction": "sideways", "amount": "5", "note": "bad direction"},
        {"direction": "credit", "amount": "5", "note": ""},
        {"direction": "credit", "amount": "5"},
        {"direction": "credit", "amount": "5", "note": "x", "reference": "revenues:1:forged"},
    ],
)
def test_api_adjustment_validation(db: Session, client: TestClient, payload: dict[str, object]) -> None:
    response = client.post("/api/v1/main-cash-account/adjustments", json=payload)
    assert response.status_code == 422
    assert logs(db) == []


def test_api_adjustments_require_superadmin(db: Session, actor: User, client: TestClient) -> None:
    db.execute(update(User).where(User.id == actor.id).values(role="ADMIN"))
    db.commit()

    response = client.post(
        "/api/v1/main-cash-account/adjustments", json={"direction": "credit", "amount": "5", "note": "x"}
    )
    assert response.status_code == 403
    assert client.get("/api/v1/main-cash-account").status_code == 200, "any active user can read"
    assert client.get("/api/v1/main-cash-account/logs").status_code == 200


def test_api_logs_list_filters_and_detail(db: Session, actor: User, client: TestClient) -> None:
    first = topup(db, actor, "1000")
    second = topup(db, actor, "200")
    moved = approve_transfer(db, actor, transfer(db, actor, "3000"))
    adjust(db, "credit", Decimal("50"), "Interest", actor)
    db.commit()

    everything = client.get("/api/v1/main-cash-account/logs")
    assert everything.status_code == 200
    data = everything.json()["data"]
    assert [entry["reference"] for entry in data] == [
        "manual:Interest",
        f"revenues:{moved.id}:transfer_approved",
        f"operational_cashflows:{second.id}:cashflow_created",
        f"operational_cashflows:{first.id}:cashflow_created",
    ], "newest first"
    assert everything.json()["meta"] == {"page": 1, "per_page": 30, "total": 4, "pages": 1}
    assert data[1]["source"] == {"type": "revenues", "id": moved.id, "action": "transfer_approved"}

    def references(**params: object) -> list[str]:
        response = client.get("/api/v1/main-cash-account/logs", params=params)
        assert response.status_code == 200, response.text
        return [entry["reference"] for entry in response.json()["data"]]

    assert len(references(direction="debit")) == 2
    assert len(references(direction="credit")) == 2
    assert len(references(source="operational_cashflows")) == 2
    assert references(source="operational_cashflows", source_id=first.id) == [
        f"operational_cashflows:{first.id}:cashflow_created"
    ]
    assert references(source="revenues") == [f"revenues:{moved.id}:transfer_approved"]
    assert references(source="manual") == ["manual:Interest"]
    today = today_local()  # from/to are business dates in APP_TIMEZONE, not the server's UTC date
    assert len(references(**{"from": today.isoformat(), "to": today.isoformat()})) == 4
    assert references(**{"from": (today + timedelta(days=1)).isoformat()}) == []
    assert references(**{"to": (today - timedelta(days=1)).isoformat()}) == []

    paged = client.get("/api/v1/main-cash-account/logs", params={"per_page": 3, "page": 2})
    assert len(paged.json()["data"]) == 1
    assert paged.json()["meta"]["pages"] == 2

    assert client.get("/api/v1/main-cash-account/logs", params={"source_id": first.id}).status_code == 422
    assert client.get("/api/v1/main-cash-account/logs", params={"source": "bogus"}).status_code == 422

    log_id = data[0]["id"]
    detail = client.get(f"/api/v1/main-cash-account/logs/{log_id}")
    assert detail.status_code == 200
    assert detail.json()["data"] == data[0]
    assert client.get("/api/v1/main-cash-account/logs/999999999").status_code == 404


def test_api_existing_endpoints_populate_the_main_account(db: Session, client: TestClient) -> None:
    """Top-up and transfer done through the existing operational-cashflow / revenue endpoints."""
    today = today_local().isoformat()
    created = client.post(
        "/api/v1/operational-cashflows",
        json={"cashflow_type": "MAIN_ACCOUNT_TOPUP", "transaction_date": today, "amount_idr": "700", "details": {}},
    )
    assert created.status_code == 201, created.text

    revenue = client.post(
        "/api/v1/revenues",
        json={
            "revenue_type": "MAIN_ACCOUNT_TRANSFER",
            "transaction_date": today,
            "details": {"gross_amount_idr": "2000", "payment_cost_idr": "10"},
        },
    )
    assert revenue.status_code == 201, revenue.text
    revenue_id = revenue.json()["data"]["id"]
    assert client.post(f"/api/v1/revenues/{revenue_id}/submit").status_code == 200
    decision = client.post(
        f"/api/v1/revenues/{revenue_id}/decision",
        json={"decision": "APPROVED", "acknowledge_negative_balance": True},
    )
    assert decision.status_code == 200, decision.text

    account = client.get("/api/v1/main-cash-account").json()["data"]
    assert account["current_amount"] == "1290"  # -700 + (2000 - 10)
    transfers = client.get(
        "/api/v1/main-cash-account/logs", params={"source": "revenues", "source_id": revenue_id}
    ).json()["data"]
    assert [(entry["direction"], entry["amount"]) for entry in transfers] == [("credit", "1990")]


# --- Backfill -----------------------------------------------------------------------------------


@pytest.fixture
def legacy_history(db: Session, actor: User) -> dict[str, int]:
    """Top-ups and transfers inserted straight into the tables, so the live sync never saw them."""

    def cashflow(payment_status: str, value: str) -> int:
        row = OperationalCashflow(
            cashflow_type="MAIN_ACCOUNT_TOPUP",
            direction="INFLOW",
            transaction_date=date(2026, 1, 10),
            amount_idr=Decimal(value),
            approval_status="NOT_REQUIRED",
            payment_status=payment_status,
            created_by=actor.id,
        )
        db.add(row)
        db.flush()
        return row.id

    def revenue(approval_status: str, payment_status: str, gross: str, cost: str) -> int:
        row = Revenue(
            revenue_type="MAIN_ACCOUNT_TRANSFER",
            direction="OUTFLOW",
            transaction_date=date(2026, 1, 12),
            amount_idr=Decimal(gross) - Decimal(cost),
            approval_status=approval_status,
            payment_status=payment_status,
            created_by=actor.id,
        )
        db.add(row)
        db.flush()
        db.add(
            RevenueTransferDetail(revenue_id=row.id, gross_amount_idr=Decimal(gross), payment_cost_idr=Decimal(cost))
        )
        return row.id

    ids = {
        "topup_a": cashflow("PAID", "1000"),  # debit 1000
        "topup_b": cashflow("PAID", "300"),  # debit 300
        "transfer_paid": revenue("APPROVED", "PAID", "5000", "100"),  # credit 4900
        "transfer_draft": revenue("DRAFT", "UNPAID", "7777", "0"),  # nothing yet
    }
    db.commit()
    return ids  # expected amount: 4900 - 1300 = 3600


def test_backfill_posts_existing_history(db: Session, legacy_history: dict[str, int]) -> None:
    result = backfill_main_cash_account(db)

    assert result.previous_amount is None
    assert result.new_amount == Decimal("3600")
    assert sorted(logs(db), key=lambda item: item[2]) == [
        ("debit", Decimal("1000"), f"operational_cashflows:{legacy_history['topup_a']}:backfill"),
        ("debit", Decimal("300"), f"operational_cashflows:{legacy_history['topup_b']}:backfill"),
        ("credit", Decimal("4900"), f"revenues:{legacy_history['transfer_paid']}:backfill"),
    ]
    assert amount(db) == Decimal("3600")


def test_backfill_is_idempotent_and_keeps_live_and_manual_entries(
    db: Session, actor: User, legacy_history: dict[str, int]
) -> None:
    backfill_main_cash_account(db)
    topup(db, actor, "200")  # live entry after the backfill
    adjust(db, "credit", Decimal("50"), "Interest", actor)
    db.commit()
    before = logs(db)

    again = backfill_main_cash_account(db)

    assert not again.changed
    assert again.posted == []
    assert logs(db) == before
    assert amount(db) == Decimal("3600") - Decimal("200") + Decimal("50")


def test_backfill_dry_run_writes_nothing(db: Session, legacy_history: dict[str, int]) -> None:
    result = backfill_main_cash_account(db, dry_run=True)

    assert result.new_amount == Decimal("3600")
    assert len(result.posted) == 3
    assert logs(db) == []
    assert amount(db) is None


def test_backfill_repairs_drift(db: Session, actor: User, legacy_history: dict[str, int]) -> None:
    backfill_main_cash_account(db)
    topup_a = db.get(OperationalCashflow, legacy_history["topup_a"])
    # Manual database edits the live sync never saw: a top-up amount changed, a transfer became paid,
    # and the stored account amount was overwritten.
    db.execute(update(OperationalCashflow).where(OperationalCashflow.id == topup_a.id).values(amount_idr=1100))
    db.execute(
        update(Revenue)
        .where(Revenue.id == legacy_history["transfer_draft"])
        .values(approval_status="APPROVED", payment_status="PAID")
    )
    db.execute(update(MainCashAccount).values(current_amount=123))
    db.commit()

    result = backfill_main_cash_account(db)

    assert result.previous_amount == Decimal("123")
    assert [(entry.direction, entry.amount, entry.reference) for entry in result.posted] == [
        ("debit", Decimal("100"), f"operational_cashflows:{topup_a.id}:backfill"),
        ("credit", Decimal("7777"), f"revenues:{legacy_history['transfer_draft']}:backfill"),
    ]
    assert amount(db) == Decimal("3600") - Decimal("100") + Decimal("7777")
    assert_log_matches_amount(db)


def test_backfill_reverses_entries_whose_source_no_longer_qualifies(
    db: Session, legacy_history: dict[str, int]
) -> None:
    backfill_main_cash_account(db)
    db.execute(
        update(OperationalCashflow)
        .where(OperationalCashflow.id == legacy_history["topup_b"])
        .values(payment_status="UNPAID")
    )
    db.commit()

    result = backfill_main_cash_account(db)

    assert [(entry.direction, entry.amount) for entry in result.posted] == [("credit", Decimal("300"))]
    assert amount(db) == Decimal("3900")


def test_cli(db: Session, legacy_history: dict[str, int], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Log entries to post:               3" in out
    assert "New main cash account amount:      3600" in out
    assert "Dry run: nothing was written." in out
    assert amount(db) is None

    assert main([]) == 0
    assert "Main cash account updated." in capsys.readouterr().out
    assert amount(db) == Decimal("3600")

    assert main([]) == 0
    assert "already up to date" in capsys.readouterr().out
