"""Checks for lender management (app/features/lender_service.py, app/features/lenders.py).

Rules under test:

- only the name is required, and it is unique regardless of case; blank optional fields are NULL;
- the lender list/detail shows every lender's ACTIVE loans with their full principal + interest,
  what has been repaid and what is still to be repaid, plus the lender totals;
- an inactive lender keeps its loans but cannot receive a new one;
- renaming a lender updates the cash statements of its loans;
- anyone can read, only a Superadmin can write.

These run against the real PostgreSQL test database (see tests/conftest.py).
"""

import os
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, CashStatement, User
from app.features.cash_statement_service import rebuild_cash_statements
from app.features.common import today_local
from app.main import app

TODAY = today_local()
D = Decimal


@pytest.fixture
def client(actor: User, actor_password: str) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/auth/login",
            headers={"X-FM-API-Key": os.environ["API_KEY"]},
            json={"email": actor.email, "password": actor_password},
        )
        assert response.status_code == 200
        test_client.headers.update(
            {
                "X-FM-API-Key": os.environ["API_KEY"],
                "Authorization": f"Bearer {response.json()['data']['access_token']}",
            }
        )
        yield test_client


def create_lender(client: TestClient, name: str, **fields: object) -> dict[str, object]:
    response = client.post("/api/v1/lenders", json={"name": name} | fields)
    assert response.status_code == 201, response.json()
    data: dict[str, object] = response.json()["data"]
    return data


def long_term_loan(lender_id: object, **overrides: object) -> dict[str, object]:
    start = TODAY - timedelta(days=120)
    return {
        "lender_id": lender_id,
        "reference_number": "KUR-001",
        "loan_type": "LONG_TERM",
        "principal_amount": "12000000",  # 12 % flat, 12 months: 1,440,000 interest
        "annual_interest_rate": "12",
        "interest_method": "FLAT",
        "term_months": 12,
        "start_date": start.isoformat(),
        "first_due_date": (start + timedelta(days=30)).isoformat(),
    } | overrides


def short_term_loan(lender_id: object, **overrides: object) -> dict[str, object]:
    return {
        "lender_id": lender_id,
        "loan_type": "SHORT_TERM",
        "principal_amount": "5000000",
        "interest_amount": "150000",
        "start_date": (TODAY - timedelta(days=10)).isoformat(),
        "due_date": (TODAY + timedelta(days=5)).isoformat(),
    } | overrides


def new_loan(client: TestClient, payload: dict[str, object]) -> dict[str, object]:
    response = client.post("/api/v1/loans", json=payload)
    assert response.status_code == 201, response.json()
    data: dict[str, object] = response.json()["data"]
    return data


def repay(client: TestClient, loan_id: object, amount: str | None = None) -> None:
    payload: dict[str, object] = {"paid_date": TODAY.isoformat()}
    if amount is not None:
        payload["amount"] = amount
    assert client.post(f"/api/v1/loans/{loan_id}/repayments", json=payload).status_code == 201


# --- Managing lenders ---------------------------------------------------------------------------


def test_only_the_name_is_required(db: Session, client: TestClient) -> None:
    lender = create_lender(client, "  Bank BRI  ")

    assert lender["name"] == "Bank BRI", "surrounding spaces are removed"
    assert {key: lender[key] for key in ("contact_person", "phone", "email", "address", "notes")} == dict.fromkeys(
        ("contact_person", "phone", "email", "address", "notes")
    )
    assert lender["is_active"] is True
    assert lender["active_loans"] == []
    assert lender["active_loans_summary"]["active_loans"] == 0
    assert lender["active_loans_summary"]["total_outstanding"] == "0"


def test_optional_fields(db: Session, client: TestClient) -> None:
    lender = create_lender(
        client,
        "Koperasi Maju",
        contact_person="Pak Budi",
        phone="0812-3456-7890",
        email="budi@koperasi.example",
        address="Jl. Raya 1, Bogor",
        notes="Monthly meeting on the 5th",
    )
    assert (lender["contact_person"], lender["phone"], lender["email"]) == (
        "Pak Budi",
        "0812-3456-7890",
        "budi@koperasi.example",
    )

    blanked = client.patch(f"/api/v1/lenders/{lender['id']}", json={"name": "Koperasi Maju", "phone": "  "})
    assert blanked.status_code == 200
    assert (blanked.json()["data"]["phone"], blanked.json()["data"]["email"]) == (None, None)


@pytest.mark.parametrize(
    "payload",
    [{}, {"name": ""}, {"name": "x" * 256}, {"name": "Bank", "email": "not-an-email"}, {"name": "Bank", "extra": 1}],
)
def test_validation(db: Session, client: TestClient, payload: dict[str, object]) -> None:
    assert client.post("/api/v1/lenders", json=payload).status_code == 422


def test_blank_name_is_rejected(db: Session, client: TestClient) -> None:
    response = client.post("/api/v1/lenders", json={"name": "   "})
    assert response.status_code == 422
    assert response.json()["errors"] == {"name": ["Name is required."]}


def test_names_are_unique_regardless_of_case(db: Session, client: TestClient) -> None:
    bri = create_lender(client, "Bank BRI")
    mandiri = create_lender(client, "Bank Mandiri")

    duplicate = client.post("/api/v1/lenders", json={"name": "bank bri"})
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "LENDER_EXISTS")
    renamed_onto = client.patch(f"/api/v1/lenders/{mandiri['id']}", json={"name": "BANK BRI"})
    assert renamed_onto.status_code == 409
    own_case = client.patch(f"/api/v1/lenders/{bri['id']}", json={"name": "Bank bri"})
    assert own_case.status_code == 200, "changing the case of its own name is fine"


def test_writes_are_audited(db: Session, client: TestClient) -> None:
    lender = create_lender(client, "Bank BRI")
    client.patch(f"/api/v1/lenders/{lender['id']}", json={"name": "Bank BRI", "is_active": False})

    actions = db.scalars(
        select(AuditEvent.action).where(AuditEvent.entity_type == "lenders").order_by(AuditEvent.id)
    ).all()
    assert actions == ["lender_created", "lender_updated"]


# --- Active loans per lender --------------------------------------------------------------------


def test_lender_shows_its_active_loans_and_what_is_left_to_repay(db: Session, client: TestClient) -> None:
    bri = create_lender(client, "Bank BRI")
    long_term = new_loan(client, long_term_loan(bri["id"]))
    repay(client, long_term["id"], "1500000")
    short_term = new_loan(client, short_term_loan(bri["id"]))
    paid_off = new_loan(client, short_term_loan(bri["id"], principal_amount="1000000", interest_amount="0"))
    repay(client, paid_off["id"])
    cancelled = new_loan(client, short_term_loan(bri["id"]))
    client.post(f"/api/v1/loans/{cancelled['id']}/cancel", json={"reason": "Entered twice"})

    lender = client.get(f"/api/v1/lenders/{bri['id']}").json()["data"]

    # Only the ACTIVE loans: the paid-off and the cancelled ones are not owed any more.
    assert [loan["id"] for loan in lender["active_loans"]] == [long_term["id"], short_term["id"]]
    first, second = lender["active_loans"]
    assert {key: first[key] for key in ("principal_amount", "total_interest", "total_payable", "total_paid")} == {
        "principal_amount": "12000000",
        "total_interest": "1440000",
        "total_payable": "13440000",
        "total_paid": "1500000",
    }
    assert first["total_outstanding"] == "11940000"
    # Interest first in each installment: 120,000 + 1,000,000 (installment 1), then 120,000 + 260,000.
    assert (first["principal_outstanding"], first["interest_outstanding"]) == ("10740000", "1200000")
    # 1,500,000 paid installment 1 (1,120,000) and 380,000 of installment 2.
    assert first["next_installment"]["sequence"] == 2 and first["next_installment"]["amount_due"] == "740000"
    assert (second["loan_type"], second["total_payable"], second["total_outstanding"]) == (
        "SHORT_TERM",
        "5150000",
        "5150000",
    )
    assert second["maturity_date"] == (TODAY + timedelta(days=5)).isoformat()
    assert second["overdue_amount"] == "0"

    summary = lender["active_loans_summary"]
    assert summary == {
        "active_loans": 2,
        "principal_amount": "17000000",
        "total_interest": "1590000",
        "total_payable": "18590000",
        "total_paid": "1500000",
        "principal_outstanding": "15740000",
        "interest_outstanding": "1350000",
        "total_outstanding": "17090000",
        "overdue_amount": first["overdue_amount"],
        "next_due_date": min(first["next_installment"]["due_date"], second["next_installment"]["due_date"]),
    }
    # The same figures as the loan itself.
    loan = client.get(f"/api/v1/loans/{long_term['id']}").json()["data"]["summary"]
    assert (loan["total_payable"], loan["total_paid"], loan["total_outstanding"]) == ("13440000", "1500000", "11940000")


def test_list_with_filters_and_active_loans(db: Session, client: TestClient) -> None:
    bri = create_lender(client, "Bank BRI", contact_person="Ibu Sari")
    create_lender(client, "Koperasi Maju")
    create_lender(client, "Arisan Keluarga", is_active=False)
    new_loan(client, long_term_loan(bri["id"]))

    listed = client.get("/api/v1/lenders").json()
    assert [item["name"] for item in listed["data"]] == ["Arisan Keluarga", "Bank BRI", "Koperasi Maju"]
    assert [item["active_loans_summary"]["active_loans"] for item in listed["data"]] == [0, 1, 0]
    assert listed["data"][1]["active_loans_summary"]["total_outstanding"] == "13440000"

    def names(**params: object) -> list[str]:
        return [item["name"] for item in client.get("/api/v1/lenders", params=params).json()["data"]]

    assert names(search="bri") == ["Bank BRI"]
    assert names(search="sari") == ["Bank BRI"], "the contact person is searched too"
    assert names(is_active=False) == ["Arisan Keluarga"]
    assert names(has_active_loans=True) == ["Bank BRI"]
    assert names(has_active_loans=False) == ["Arisan Keluarga", "Koperasi Maju"]
    paged = client.get("/api/v1/lenders", params={"per_page": 2, "page": 2}).json()
    assert (paged["meta"]["total"], [item["name"] for item in paged["data"]]) == (3, ["Koperasi Maju"])
    assert client.get("/api/v1/lenders/999999").status_code == 404


# --- Lenders and loans --------------------------------------------------------------------------


def test_inactive_lender_keeps_its_loans_but_gets_no_new_one(db: Session, client: TestClient) -> None:
    bri = create_lender(client, "Bank BRI")
    existing = new_loan(client, long_term_loan(bri["id"]))
    other = create_lender(client, "Bank Mandiri")
    client.patch(f"/api/v1/lenders/{bri['id']}", json={"name": "Bank BRI", "is_active": False})

    refused = client.post("/api/v1/loans", json=short_term_loan(bri["id"]))
    assert refused.status_code == 422
    assert refused.json()["errors"] == {"lender_id": ["Lender is inactive; reactivate it to record a new loan."]}
    moved = client.patch(f"/api/v1/loans/{existing['id']}", json=long_term_loan(other["id"]))
    assert moved.status_code == 200
    moved_back = client.patch(f"/api/v1/loans/{existing['id']}", json=long_term_loan(bri["id"]))
    assert moved_back.status_code == 422, "moving a loan to an inactive lender is a new loan for it"

    # Its existing loan can still be edited and repaid.
    own = new_loan(client, long_term_loan(other["id"]))
    client.patch(f"/api/v1/lenders/{other['id']}", json={"name": "Bank Mandiri", "is_active": False})
    assert client.patch(f"/api/v1/loans/{own['id']}", json=long_term_loan(other["id"], notes="x")).status_code == 200
    repay(client, own["id"])

    unknown = client.post("/api/v1/loans", json=short_term_loan(999999))
    assert unknown.json()["errors"] == {"lender_id": ["Lender does not exist."]}


def test_renaming_a_lender_updates_the_cash_statements_of_its_loans(db: Session, client: TestClient) -> None:
    lender = create_lender(client, "Bank BRI")
    loan = new_loan(client, long_term_loan(lender["id"]))
    repay(client, loan["id"])
    assert loan["lender"] == {"id": lender["id"], "name": "Bank BRI"}

    client.patch(f"/api/v1/lenders/{lender['id']}", json={"name": "Bank Rakyat Indonesia"})

    # cash_statements picks up the rename once it is (re)synced - once a night in production (see
    # app.scripts.sync_cash_statements), simulated here with the full rebuild.
    rebuild_cash_statements(db)
    db.expire_all()
    row = db.scalars(select(CashStatement)).one()
    assert (row.description, row.counterparty) == (
        "Loan repayment: Bank Rakyat Indonesia KUR-001 (installment 1)",
        "Bank Rakyat Indonesia",
    )
    detail = client.get(f"/api/v1/loans/{loan['id']}").json()["data"]
    assert detail["lender"]["name"] == "Bank Rakyat Indonesia"


def test_anyone_can_read_only_superadmin_can_write(db: Session, client: TestClient, actor: User) -> None:
    lender = create_lender(client, "Bank BRI")
    new_loan(client, long_term_loan(lender["id"]))
    db.execute(update(User).where(User.id == actor.id).values(role="ADMIN"))
    db.commit()

    assert client.get("/api/v1/lenders").status_code == 200
    detail = client.get(f"/api/v1/lenders/{lender['id']}")
    assert detail.status_code == 200 and len(detail.json()["data"]["active_loans"]) == 1
    assert client.post("/api/v1/lenders", json={"name": "Bank Mandiri"}).status_code == 403
    assert client.patch(f"/api/v1/lenders/{lender['id']}", json={"name": "x"}).status_code == 403
