"""Checks for asynchronous main cash account posting (transactional outbox).

Cashflow/revenue transactions only queue their main account movement in
main_cash_account_events; a background worker (FastAPI background task, the worker command, or
the daily audit) posts the queue to main_cash_account_logs. These tests check that:

- the API transaction neither waits for nor locks the main cash account;
- the queue is posted exactly once, with the same ledger synchronous posting produced;
- the audit treats queued movements as already accounted for and never blocks writers.
"""

import os
import threading
import time
from collections.abc import Callable
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import MainCashAccountEvent, MainCashAccountLog, User
from app.db.session import SessionLocal, engine
from app.features.common import today_local
from app.features.finance_service import decide_revenue, save_operational, save_revenue, submit_revenue
from app.features.locks import ADVISORY_LOCK
from app.features.main_cash_account_audit import audit_main_cash_account
from app.features.main_cash_account_events import drain, pending_summary, process_batch
from app.features.main_cash_account_service import get_account, get_or_create_account
from app.main import app
from app.scripts.main_cash_account_worker import main as worker_main


def topup_data(value: str) -> dict[str, object]:
    return {
        "cashflow_type": "MAIN_ACCOUNT_TOPUP",
        "transaction_date": today_local(),
        "amount_idr": Decimal(value),
        "details": {"notes": "Top up"},
    }


def transfer_data(gross: str) -> dict[str, object]:
    return {
        "revenue_type": "MAIN_ACCOUNT_TRANSFER",
        "transaction_date": today_local(),
        "details": {"gross_amount_idr": Decimal(gross), "payment_cost_idr": Decimal("0"), "notes": None},
    }


def queued(db: Session) -> list[tuple[str, int, str, Decimal]]:
    db.expire_all()
    rows = db.scalars(select(MainCashAccountEvent).order_by(MainCashAccountEvent.id)).all()
    return [(row.source_table, row.source_id, row.action, Decimal(row.delta)) for row in rows]


def posted(db: Session) -> list[tuple[str, Decimal, str]]:
    db.expire_all()
    rows = db.scalars(select(MainCashAccountLog).order_by(MainCashAccountLog.id)).all()
    return [(row.direction, Decimal(row.amount), row.reference) for row in rows]


def amount(db: Session) -> Decimal | None:
    db.expire_all()
    account = get_account(db)
    return None if account is None else Decimal(account.current_amount)


def seed_account(db: Session) -> None:
    get_or_create_account(db)
    db.commit()


class RowLockHolder:
    """Holds a lock in another connection, like a long main account operation would."""

    def __init__(self, sql: str) -> None:
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.connection.execute(text(sql))

    def release(self) -> None:
        self.transaction.rollback()
        self.connection.close()


def finishes_within(seconds: float, work: Callable[[], object]) -> bool:
    """Runs ``work`` in a thread; True if it completes before the timeout (it is not blocked)."""
    errors: list[BaseException] = []

    def target() -> None:
        try:
            work()
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if errors:
        raise errors[0]
    return not thread.is_alive()


# --- The write path only queues -----------------------------------------------------------------


def test_a_topup_is_queued_not_posted(db: Session, actor: User) -> None:
    row = save_operational(db, topup_data("1000"), actor)
    db.commit()

    assert queued(db) == [("operational_cashflows", row.id, "cashflow_created", Decimal("-1000"))]
    assert posted(db) == [], "nothing posted inside the API transaction"
    assert amount(db) is None, "the API transaction did not even touch the account row"
    assert pending_summary(db) == (1, Decimal("-1000"))

    assert drain() == 1

    assert queued(db) == []
    assert posted(db) == [("debit", Decimal("1000"), f"operational_cashflows:{row.id}:cashflow_created")]
    assert amount(db) == Decimal("-1000")


def test_posted_entry_keeps_the_time_of_the_change(db: Session, actor: User) -> None:
    save_operational(db, topup_data("1000"), actor)
    db.commit()
    event_time = db.scalar(select(MainCashAccountEvent.created_at))
    time.sleep(0.05)

    drain()

    assert db.scalar(select(MainCashAccountLog.created_at)) == event_time


def test_changes_queued_before_the_worker_runs_keep_their_own_entries(db: Session, actor: User) -> None:
    """Same ledger as synchronous posting: one entry per change, not one netted entry."""
    row = save_operational(db, topup_data("1000"), actor)
    db.commit()
    save_operational(db, topup_data("800"), actor, row, correction=True, reason="typo")
    db.commit()
    transfer = save_revenue(db, transfer_data("5000"), actor)
    db.commit()
    submit_revenue(db, transfer, actor)
    decide_revenue(db, transfer, actor, "APPROVED", None, True)
    db.commit()

    assert len(queued(db)) == 3
    drain()

    assert posted(db) == [
        ("debit", Decimal("1000"), f"operational_cashflows:{row.id}:cashflow_created"),
        ("credit", Decimal("200"), f"operational_cashflows:{row.id}:cashflow_corrected"),
        ("credit", Decimal("5000"), f"revenues:{transfer.id}:transfer_approved"),
    ]
    assert amount(db) == Decimal("4200")


def test_a_cashflow_write_does_not_wait_for_a_locked_main_account(db: Session, actor: User) -> None:
    seed_account(db)
    holder = RowLockHolder("SELECT id FROM main_cash_accounts FOR UPDATE")
    try:
        # If the transaction needed the account row it would fail after 1 s instead of waiting.
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        started = time.monotonic()
        save_operational(db, topup_data("1000"), actor)
        db.commit()
        assert time.monotonic() - started < 1
    finally:
        holder.release()

    assert len(queued(db)) == 1
    drain()
    assert amount(db) == Decimal("-1000")


def test_a_transfer_approval_does_not_wait_for_a_locked_main_account(db: Session, actor: User) -> None:
    seed_account(db)
    row = save_revenue(db, transfer_data("3000"), actor)
    db.commit()
    submit_revenue(db, row, actor)
    db.commit()
    holder = RowLockHolder("SELECT id FROM main_cash_accounts FOR UPDATE")
    try:
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        decide_revenue(db, row, actor, "APPROVED", None, True)
        db.commit()
    finally:
        holder.release()

    drain()
    assert amount(db) == Decimal("3000")


# --- The worker ---------------------------------------------------------------------------------


def test_worker_posts_in_batches(db: Session, actor: User) -> None:
    for value in ("100", "200", "300", "400", "500"):
        save_operational(db, topup_data(value), actor)
        db.commit()

    with SessionLocal() as worker:
        assert process_batch(worker, batch_size=2) == 2
    assert len(queued(db)) == 3
    assert amount(db) == Decimal("-300"), "each batch is committed on its own"

    assert drain(batch_size=2, max_batches=1) == 2
    assert drain(batch_size=2) == 1
    assert queued(db) == []
    assert amount(db) == Decimal("-1500")


def test_concurrent_workers_post_every_event_exactly_once(db: Session, actor: User) -> None:
    for index in range(40):
        save_operational(db, topup_data(str(100 + index)), actor)
        db.commit()
    expected = -sum(Decimal(100 + index) for index in range(40))

    threads = [threading.Thread(target=drain, kwargs={"batch_size": 3}) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert queued(db) == []
    assert len(posted(db)) == 40
    assert amount(db) == expected


def test_worker_command(db: Session, actor: User) -> None:
    save_operational(db, topup_data("700"), actor)
    db.commit()

    assert worker_main(["--once"]) == 0

    assert queued(db) == []
    assert amount(db) == Decimal("-700")
    with pytest.raises(SystemExit):
        worker_main(["--batch-size", "0"])


# --- Through the API ----------------------------------------------------------------------------


@pytest.fixture
def client(actor: User, actor_password: str) -> TestClient:
    test_client = TestClient(app)
    login = test_client.post(
        "/api/v1/auth/login",
        headers={"X-FM-API-Key": os.environ["API_KEY"]},
        json={"email": actor.email, "password": actor_password},
    )
    test_client.headers.update(
        {
            "X-FM-API-Key": os.environ["API_KEY"],
            "Authorization": f"Bearer {login.json()['data']['access_token']}",
        }
    )
    return test_client


def test_api_posts_in_a_background_task_after_the_response(db: Session, client: TestClient) -> None:
    response = client.post(
        "/api/v1/operational-cashflows",
        json={
            "cashflow_type": "MAIN_ACCOUNT_TOPUP",
            "transaction_date": today_local().isoformat(),
            "amount_idr": "900",
            "details": {},
        },
    )
    assert response.status_code == 201

    # TestClient runs background tasks before returning, so the queue has been drained here.
    assert queued(db) == []
    account = client.get("/api/v1/main-cash-account").json()["data"]
    assert (account["current_amount"], account["pending_entries"], account["pending_amount"]) == ("-900", 0, "0")


def test_api_response_does_not_depend_on_the_background_posting(
    db: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.features.main_cash_account_events as events

    def broken(**kwargs: object) -> int:
        raise RuntimeError("main account posting is down")

    monkeypatch.setattr(events, "drain", broken)
    response = client.post(
        "/api/v1/operational-cashflows",
        json={
            "cashflow_type": "MAIN_ACCOUNT_TOPUP",
            "transaction_date": today_local().isoformat(),
            "amount_idr": "900",
            "details": {},
        },
    )

    assert response.status_code == 201, "the cashflow is saved even though posting failed"
    account = client.get("/api/v1/main-cash-account").json()["data"]
    assert (account["current_amount"], account["pending_entries"], account["pending_amount"]) == ("0", 1, "-900")

    monkeypatch.undo()
    drain()  # the next trigger picks it up
    assert amount(db) == Decimal("-900")


# --- The audit and the queue --------------------------------------------------------------------


def test_audit_counts_queued_movements_as_accounted_for(db: Session, actor: User) -> None:
    save_operational(db, topup_data("1000"), actor)
    db.commit()

    dry = audit_main_cash_account(dry_run=True, session=db)
    assert dry.discrepancies == 0, "a queued movement is not missing"
    assert dry.new_amount == Decimal("-1000"), "projection includes the queue"
    assert len(queued(db)) == 1, "a dry run does not drain"

    result = audit_main_cash_account(session=db)
    assert result.events_processed == 1, "a real run drains the queue first (safety net)"
    assert result.discrepancies == 0
    assert queued(db) == []
    assert amount(db) == Decimal("-1000")


def test_audit_does_not_block_on_the_cashflow_write_lock(db: Session, actor: User) -> None:
    save_operational(db, topup_data("1000"), actor)
    db.commit()
    drain()
    # Another transaction is in the middle of a cashflow/revenue write (holds the write lock).
    holder = RowLockHolder(f"SELECT pg_advisory_xact_lock({ADVISORY_LOCK})")
    try:
        assert finishes_within(10, lambda: audit_main_cash_account()), "the audit was blocked by the writers' lock"
    finally:
        holder.release()
