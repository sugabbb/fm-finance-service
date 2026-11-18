import os
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db.models import User

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/fm_finance_test")
os.environ.setdefault("API_KEY", "test-api-key-with-at-least-24-characters")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-with-at-least-thirty-two-characters")
os.environ.setdefault("OPERATIONAL_INTERNAL_API_KEY", "test-internal-key-with-at-least-24-characters")


# --- Timezone: the tests run in APP_TIMEZONE ----------------------------------------------------
# The app derives every business date in APP_TIMEZONE (app.features.common.today_local), whatever
# the timezone of the machine. The tests take their own dates with today_local() too, and on top
# of that this process is switched to APP_TIMEZONE, so any naive local time (date.today(),
# datetime.now(), a library's) agrees with the app. Without it, a machine whose timezone differs
# from APP_TIMEZONE fails date assertions for part of every day (e.g. UTC vs Asia/Jakarta between
# 17:00 and 24:00 UTC, when Jakarta is already on the next date).
# Done at import, before any test module computes its module-level dates.


def use_app_timezone() -> None:
    from datetime import datetime

    from app.core.config import settings

    os.environ["TZ"] = settings.app_timezone
    if hasattr(time, "tzset"):  # POSIX; elsewhere the tests still use today_local() explicitly
        time.tzset()
        now = datetime.now(settings.timezone)
        if abs(now.replace(tzinfo=None) - datetime.now()) > timedelta(minutes=1):
            raise pytest.UsageError(f"could not switch the test process to APP_TIMEZONE={settings.app_timezone}")


use_app_timezone()


# Finance intentionally has no production login endpoint. Legacy endpoint-flow tests still need a
# token, so the test application gets a hidden issuer while all authorization continues through
# the same Bearer dependency used in production.
def install_test_login() -> None:
    from fastapi import HTTPException
    from pydantic import BaseModel
    from sqlalchemy import select

    from app.core.security import create_access_token
    from app.db.models import User
    from app.db.session import SessionLocal
    from app.main import app

    class LoginInput(BaseModel):
        email: str
        password: str

    @app.post("/api/v1/auth/login", include_in_schema=False)
    def test_login(payload: LoginInput) -> dict[str, object]:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == payload.email))
            if not user:
                raise HTTPException(401)
            token, expires = create_access_token(user.id, user.token_version)
            return {"data": {"access_token": token, "expires_at": expires.isoformat()}}


install_test_login()


@pytest.fixture(autouse=True)
def operational_authority_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.session import SessionLocal

    def authorize(user_id: int, token_version: int) -> dict[str, object]:
        from app.db.models import User

        with SessionLocal() as db:
            user = db.get(User, user_id)
            if not user or user.token_version != token_version:
                raise AssertionError("test token does not match the projected user")
            return {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "role": user.role,
                "is_active": user.is_active,
                "must_change_password": user.must_change_password,
                "token_version": user.token_version,
            }

    monkeypatch.setattr("app.integrations.operational.operational_client.authorize", authorize)


def pytest_sessionstart(session: pytest.Session) -> None:
    database_url = os.environ["DATABASE_URL"]
    database_name = urlparse(database_url.replace("postgresql+psycopg", "postgresql", 1)).path.rsplit("/", 1)[-1]
    if not database_name.endswith("_test"):
        raise pytest.UsageError("DATABASE_URL must target a database whose name ends in _test")


# --- Shared database fixtures for the cashflow / revenue / balance tests ----------------------
# App modules are imported lazily so the environment defaults above are applied first.

TEST_USER_EMAIL = "cash-balance-tests@example.com"
TEST_PASSWORD = "StrongPassword123"


def wipe_cashflow_data(db: Session) -> None:
    from sqlalchemy import delete

    from app.db.models import (
        AuditEvent,
        CashStatement,
        CashStatementSyncRun,
        Customer,
        FinancialOpeningBalance,
        Lender,
        Loan,
        LoanRepayment,
        MainCashAccount,
        MainCashAccountAuditRun,
        MainCashAccountEvent,
        MainCashAccountLog,
        MonthlyCutoff,
        OperationalCashflow,
        OperationalCashflowBalance,
        OperationalCashflowPayment,
        OperationalCashflowTopupDetail,
        OperationalCashflowUtilityDetail,
        OperationalIntegrationOutbox,
        Revenue,
        User,
    )

    db.execute(
        delete(AuditEvent).where(
            AuditEvent.entity_type.in_(
                (
                    "operational_cashflows",
                    "revenues",
                    "main_cash_account_logs",
                    "financial_opening_balances",
                    "customers",
                    "loans",
                    "lenders",
                )
            )
        )
    )
    db.execute(delete(MonthlyCutoff))  # details cascade
    db.execute(delete(CashStatement))
    db.execute(delete(CashStatementSyncRun))
    db.execute(delete(LoanRepayment))
    db.execute(delete(Loan))  # installments cascade
    db.execute(delete(Lender))
    db.execute(delete(FinancialOpeningBalance))
    db.execute(delete(OperationalIntegrationOutbox))
    db.execute(delete(OperationalCashflowPayment))
    db.execute(delete(OperationalCashflowTopupDetail))
    db.execute(delete(OperationalCashflowUtilityDetail))
    db.execute(delete(OperationalCashflow))
    db.execute(delete(OperationalCashflowBalance))
    db.execute(delete(Revenue))  # revenue details and payments cascade
    db.execute(delete(Customer))
    db.execute(delete(MainCashAccountAuditRun))
    db.execute(delete(MainCashAccountEvent))
    db.execute(delete(MainCashAccountLog))
    db.execute(delete(MainCashAccount))
    db.execute(delete(AuditEvent).where(AuditEvent.entity_type == "users"))
    db.execute(delete(User).where(User.email == TEST_USER_EMAIL))
    db.commit()


@pytest.fixture
def db() -> Iterator[Session]:
    from app.db.session import SessionLocal

    session = SessionLocal()
    wipe_cashflow_data(session)
    try:
        yield session
    finally:
        session.rollback()
        wipe_cashflow_data(session)
        session.close()


@pytest.fixture
def actor_password() -> str:
    return TEST_PASSWORD


@pytest.fixture
def actor(db: Session) -> User:
    from app.core.security import hash_password
    from app.db.models import User

    user = User(
        name="Finance Admin",
        email=TEST_USER_EMAIL,
        password_hash=hash_password(TEST_PASSWORD),
        role="SUPERADMIN",
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
