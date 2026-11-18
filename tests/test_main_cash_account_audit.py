"""Checks for the daily main cash account audit (app/scripts/audit_main_cash_account.py).

The audit cross-checks main_cash_account_logs against operational_cashflows and revenues in both
directions, posts one compensating entry per difference, and recalculates current_amount.

These tests run against the real PostgreSQL test database (see tests/conftest.py) because the
audit is deliberately implemented in SQL (FULL OUTER JOIN, generated columns, partial indexes,
advisory locks).
"""

import os
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    MainCashAccount,
    MainCashAccountAuditRun,
    MainCashAccountLog,
    OperationalCashflow,
    Revenue,
    User,
)
from app.db.session import engine
from app.features.common import now_utc, today_local
from app.features.finance_service import decide_revenue, save_operational, save_revenue, submit_revenue
from app.features.main_cash_account_audit import (
    FULL,
    INCREMENTAL,
    audit_main_cash_account,
    try_job_lock,
)
from app.features.main_cash_account_events import drain
from app.features.main_cash_account_service import adjust, get_account
from app.main import app
from app.scripts.audit_main_cash_account import (
    EXIT_ALREADY_RUNNING,
    EXIT_DISCREPANCIES,
    EXIT_OK,
    main,
)

# --- Helpers ------------------------------------------------------------------------------------


def topup(db: Session, actor: User, value: str) -> OperationalCashflow:
    row = save_operational(
        db,
        {
            "cashflow_type": "MAIN_ACCOUNT_TOPUP",
            "transaction_date": today_local(),
            "amount_idr": Decimal(value),
            "details": {"notes": "Top up"},
        },
        actor,
    )
    db.commit()
    drain()  # the background worker has caught up
    return row


def transfer(db: Session, actor: User, gross: str, cost: str = "0") -> Revenue:
    row = save_revenue(
        db,
        {
            "revenue_type": "MAIN_ACCOUNT_TRANSFER",
            "transaction_date": today_local(),
            "details": {"gross_amount_idr": Decimal(gross), "payment_cost_idr": Decimal(cost), "notes": None},
        },
        actor,
    )
    db.commit()
    submit_revenue(db, row, actor)
    db.commit()
    row = decide_revenue(db, row, actor, "APPROVED", None, True)
    db.commit()
    drain()  # the background worker has caught up
    return row


def amount(db: Session) -> Decimal | None:
    db.expire_all()
    account = get_account(db)
    return None if account is None else Decimal(account.current_amount)


def references(db: Session) -> list[str]:
    db.expire_all()
    return list(db.scalars(select(MainCashAccountLog.reference).order_by(MainCashAccountLog.id)).all())


def run(db: Session, **kwargs: object) -> object:
    return audit_main_cash_account(session=db, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def history(db: Session, actor: User) -> dict[str, int]:
    """A healthy main cash account: two top-ups, one transfer and one manual adjustment."""
    first = topup(db, actor, "1000")
    second = topup(db, actor, "400")
    moved = transfer(db, actor, "5000", "100")
    adjust(db, "credit", Decimal("250"), "Opening balance", actor)
    db.commit()
    # -1000 - 400 + 4900 + 250
    assert amount(db) == Decimal("3750")
    return {"first": first.id, "second": second.id, "transfer": moved.id}


# --- Healthy data -------------------------------------------------------------------------------


def test_audit_of_consistent_data_posts_nothing(db: Session, history: dict[str, int]) -> None:
    result = run(db)

    assert result.status == "SUCCESS"
    assert result.discrepancies == 0
    assert result.entries_posted == 0
    assert result.sources_checked == 3, "two top-ups and one transfer; the manual entry has no source"
    assert result.previous_amount == result.new_amount == Decimal("3750")
    assert not result.changed
    assert len(references(db)) == 4


def test_audit_is_idempotent(db: Session, history: dict[str, int]) -> None:
    run(db)
    before = references(db)

    second = run(db)

    assert second.entries_posted == 0
    assert references(db) == before
    assert amount(db) == Decimal("3750")


# --- Source -> log: things missing from the log -------------------------------------------------


def test_audit_reposts_a_missing_log_entry(db: Session, history: dict[str, int]) -> None:
    db.execute(delete(MainCashAccountLog).where(MainCashAccountLog.source_id == history["first"]))
    db.execute(update(MainCashAccount).values(current_amount=Decimal("4750")))
    db.commit()

    result = run(db)

    assert result.discrepancies == 1
    assert result.entries_posted == 1
    assert f"operational_cashflows:{history['first']}:audit" in references(db)
    assert amount(db) == Decimal("3750"), "the balance is back to the truth"
    assert result.debit_posted == Decimal("1000")


def test_audit_posts_a_source_row_the_live_sync_never_saw(db: Session, actor: User, history: dict[str, int]) -> None:
    db.add(
        OperationalCashflow(
            cashflow_type="MAIN_ACCOUNT_TOPUP",
            direction="INFLOW",
            transaction_date=today_local(),
            amount_idr=Decimal("777"),
            approval_status="NOT_REQUIRED",
            payment_status="PAID",
            created_by=actor.id,
        )
    )
    db.commit()

    result = run(db)

    assert result.entries_posted == 1
    assert amount(db) == Decimal("3750") - Decimal("777")


def test_audit_corrects_an_amount_changed_behind_the_api(db: Session, history: dict[str, int]) -> None:
    db.execute(update(OperationalCashflow).where(OperationalCashflow.id == history["first"]).values(amount_idr=1250))
    db.commit()

    result = run(db)

    assert result.entries_posted == 1
    assert result.debit_posted == Decimal("250")
    assert amount(db) == Decimal("3500")


# --- Log -> source: things in the log that are no longer justified ------------------------------


def test_audit_reverses_an_entry_whose_source_stopped_qualifying(db: Session, history: dict[str, int]) -> None:
    db.execute(update(Revenue).where(Revenue.id == history["transfer"]).values(payment_status="UNPAID"))
    db.commit()

    result = run(db)

    assert result.entries_posted == 1
    assert result.debit_posted == Decimal("4900")
    assert amount(db) == Decimal("-1150")


def test_audit_reverses_an_entry_whose_source_row_is_gone(db: Session, history: dict[str, int]) -> None:
    db.execute(delete(OperationalCashflow).where(OperationalCashflow.id == history["second"]))
    db.commit()

    result = run(db)

    assert result.entries_posted == 1
    assert result.credit_posted == Decimal("400"), "the debit is given back"
    assert amount(db) == Decimal("4150")


def test_audit_reverses_a_forged_log_entry(db: Session, history: dict[str, int]) -> None:
    account = get_account(db)
    db.execute(
        insert(MainCashAccountLog).values(
            main_cash_account_id=account.id,
            amount=Decimal("9999"),
            direction="credit",
            reference=f"revenues:{history['transfer']}:forged",
        )
    )
    db.commit()

    result = run(db)

    assert result.entries_posted == 1
    assert result.debit_posted == Decimal("9999")
    assert amount(db) == Decimal("3750")


def test_audit_never_touches_manual_adjustments(db: Session, actor: User, history: dict[str, int]) -> None:
    adjust(db, "debit", Decimal("75"), "Bank fee", actor)
    db.commit()

    result = run(db)

    assert result.entries_posted == 0
    assert "manual:Bank fee" in references(db)
    assert amount(db) == Decimal("3675")


# --- Batching -----------------------------------------------------------------------------------


@pytest.mark.parametrize("batch_size", [1, 2, 3, 1000])
def test_batching_gives_the_same_result(db: Session, actor: User, batch_size: int) -> None:
    """Keyset batches must not skip or double count across boundaries, including sparse ids,
    sources with no log and logs with no source."""
    kept = topup(db, actor, "100")
    dropped = topup(db, actor, "200")
    moved = transfer(db, actor, "3000")
    adjust(db, "credit", Decimal("10"), "Opening", actor)
    db.commit()
    db.execute(delete(MainCashAccountLog).where(MainCashAccountLog.source_id == dropped.id))
    db.execute(update(Revenue).where(Revenue.id == moved.id).values(amount_idr=2500))
    db.execute(update(MainCashAccount).values(current_amount=0))
    db.commit()

    result = run(db, batch_size=batch_size)

    assert result.discrepancies == 2
    assert result.sources_checked == 3
    assert result.batches >= (3 if batch_size == 1 else 1)
    assert amount(db) == Decimal("-100") - Decimal("200") + Decimal("2500") + Decimal("10")
    assert kept.id  # silence unused
    assert run(db, batch_size=batch_size).entries_posted == 0


# --- Incremental mode ---------------------------------------------------------------------------


def test_incremental_audits_only_the_recent_window(db: Session, actor: User, history: dict[str, int]) -> None:
    run(db)  # establishes the watermark
    old_reference = f"operational_cashflows:{history['first']}:cashflow_created"
    # An old row broken behind the API, backdated so it falls outside the incremental window.
    db.execute(delete(MainCashAccountLog).where(MainCashAccountLog.reference == old_reference))
    db.execute(
        update(OperationalCashflow)
        .where(OperationalCashflow.id == history["first"])
        .values(updated_at=now_utc() - timedelta(days=30))
    )
    db.commit()

    incremental = run(db, mode=INCREMENTAL)

    assert incremental.mode == INCREMENTAL
    assert incremental.window_from is not None
    assert incremental.entries_posted == 0, "the old row is outside the window"
    assert old_reference not in references(db)

    full = run(db, mode=FULL)

    assert full.entries_posted == 1, "the weekly full sweep catches it"
    assert amount(db) == Decimal("3750")


def test_incremental_catches_recent_changes(db: Session, actor: User, history: dict[str, int]) -> None:
    run(db)
    fresh = topup(db, actor, "600")
    db.execute(delete(MainCashAccountLog).where(MainCashAccountLog.source_id == fresh.id))
    db.commit()

    result = run(db, mode=INCREMENTAL)

    assert result.entries_posted == 1
    assert amount(db) == Decimal("3750") - Decimal("600")


def test_incremental_without_a_previous_run_falls_back_to_full(db: Session, history: dict[str, int]) -> None:
    db.execute(delete(MainCashAccountLog).where(MainCashAccountLog.source_id == history["second"]))
    db.commit()

    result = run(db, mode=INCREMENTAL)

    assert result.mode == FULL
    assert result.window_from is None
    assert result.entries_posted == 1


def test_incremental_recalculates_the_amount_from_the_checkpoint(
    db: Session, actor: User, history: dict[str, int]
) -> None:
    """The log is append only, so an incremental run adds up only the entries written since the
    previous run instead of the whole table."""
    first = run(db)
    assert first.log_high_id is not None
    topup(db, actor, "300")  # live entry after the checkpoint
    adjust(db, "credit", Decimal("25"), "Interest", actor)
    db.commit()

    result = run(db, mode=INCREMENTAL)

    assert result.new_amount == Decimal("3750") - Decimal("300") + Decimal("25")
    assert amount(db) == result.new_amount
    assert result.log_high_id > first.log_high_id


def test_incremental_window_uses_the_overlap(db: Session, history: dict[str, int]) -> None:
    first = run(db)
    result = run(db, mode=INCREMENTAL, overlap=timedelta(hours=2))

    assert result.window_from is not None
    assert first.started_at is not None
    assert result.window_from == first.started_at - timedelta(hours=2)


# --- Run bookkeeping, locking and dry run --------------------------------------------------------


def test_run_is_recorded(db: Session, history: dict[str, int]) -> None:
    db.execute(delete(MainCashAccountLog).where(MainCashAccountLog.source_id == history["second"]))
    db.commit()

    result = run(db)

    db.expire_all()
    stored = db.scalar(select(MainCashAccountAuditRun).where(MainCashAccountAuditRun.id == result.run_id))
    assert stored is not None
    assert (stored.status, stored.mode, stored.dry_run) == ("SUCCESS", FULL, False)
    assert stored.discrepancies == 1
    assert stored.entries_posted == 1
    assert stored.sources_checked == 3
    assert stored.new_amount == Decimal("3750")
    assert stored.finished_at is not None and stored.log_high_id is not None
    assert stored.error is None


def test_dry_run_reports_without_writing(db: Session, history: dict[str, int]) -> None:
    db.execute(delete(MainCashAccountLog).where(MainCashAccountLog.source_id == history["second"]))
    db.execute(update(MainCashAccount).values(current_amount=Decimal("4150")))
    db.commit()
    before = references(db)

    result = run(db, dry_run=True)

    assert result.discrepancies == 1
    assert result.new_amount == Decimal("3750"), "projected amount"
    assert references(db) == before, "nothing posted"
    assert amount(db) == Decimal("4150"), "amount untouched"
    stored = db.scalar(select(MainCashAccountAuditRun).where(MainCashAccountAuditRun.id == result.run_id))
    assert stored.dry_run is True
    # A dry run must not become the watermark of the next incremental run.
    assert run(db, mode=INCREMENTAL).mode == FULL


def test_a_second_run_is_skipped_while_one_is_in_progress(db: Session, history: dict[str, int]) -> None:
    holder = engine.connect()
    try:
        assert try_job_lock(holder) is True

        result = run(db)

        assert result.status == "SKIPPED"
        assert result.run_id is None
        assert result.entries_posted == 0
    finally:
        holder.exec_driver_sql("select pg_advisory_unlock_all()")
        holder.close()

    assert run(db).status == "SUCCESS", "the lock is released again afterwards"


def test_failure_is_recorded_and_raised(db: Session, history: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    import app.features.main_cash_account_audit as audit_module

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("comparison exploded")

    monkeypatch.setattr(audit_module, "compare", boom)
    with pytest.raises(RuntimeError):
        run(db)

    db.expire_all()
    stored = db.scalars(select(MainCashAccountAuditRun).order_by(MainCashAccountAuditRun.id.desc())).first()
    assert stored.status == "FAILED"
    assert "comparison exploded" in stored.error
    assert amount(db) == Decimal("3750"), "a failed audit leaves the account alone"

    monkeypatch.undo()
    assert run(db).status == "SUCCESS", "the job lock was released despite the failure"


# --- CLI ----------------------------------------------------------------------------------------


def test_cli(db: Session, history: dict[str, int], capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Status:              SUCCESS" in out
    assert "Discrepancies found: 0" in out

    db.execute(delete(MainCashAccountLog).where(MainCashAccountLog.source_id == history["second"]))
    db.commit()

    assert main(["--dry-run", "--fail-on-discrepancy"]) == EXIT_DISCREPANCIES
    out = capsys.readouterr().out
    assert "(dry run)" in out
    assert "Entries posted:      1" in out
    assert f"fixed operational_cashflows:{history['second']}" in out
    assert len(references(db)) == 3, "dry run wrote nothing"

    assert main(["--mode", "incremental", "--batch-size", "1"]) == EXIT_OK
    assert len(references(db)) == 4
    assert amount(db) == Decimal("3750")

    assert main(["--mode", "incremental"]) == EXIT_OK
    assert capsys.readouterr().out.count("Discrepancies found: 0") == 1


def test_cli_reports_a_run_in_progress(db: Session, history: dict[str, int]) -> None:
    holder = engine.connect()
    try:
        assert try_job_lock(holder) is True
        assert main([]) == EXIT_ALREADY_RUNNING
    finally:
        holder.exec_driver_sql("select pg_advisory_unlock_all()")
        holder.close()


def test_cli_rejects_a_bad_batch_size(db: Session) -> None:
    with pytest.raises(SystemExit):
        main(["--batch-size", "0"])


# --- API ----------------------------------------------------------------------------------------


def test_api_lists_audit_runs(db: Session, actor: User, actor_password: str, history: dict[str, int]) -> None:
    run(db)
    run(db, mode=INCREMENTAL)

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-FM-API-Key": os.environ["API_KEY"]},
            json={"email": actor.email, "password": actor_password},
        )
        headers = {
            "X-FM-API-Key": os.environ["API_KEY"],
            "Authorization": f"Bearer {login.json()['data']['access_token']}",
        }
        response = client.get("/api/v1/main-cash-account/audit-runs", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["total"] == 2
        assert [entry["mode"] for entry in body["data"]] == ["incremental", "full"], "newest first"
        assert body["data"][0]["status"] == "SUCCESS"
        assert body["data"][0]["new_amount"] == "3750"

        filtered = client.get(
            "/api/v1/main-cash-account/audit-runs", headers=headers, params={"mode": "full", "status": "SUCCESS"}
        )
        assert filtered.json()["meta"]["total"] == 1
        assert (
            client.get("/api/v1/main-cash-account/audit-runs", headers=headers, params={"mode": "x"}).status_code == 422
        )
