from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query, status
from pydantic import Field
from sqlalchemy import func, select

from app.core.config import settings
from app.core.decimal import decimal_string, money
from app.core.dependencies import ActiveUser, DbSession, Superadmin
from app.core.errors import not_found, validation
from app.core.pagination import PageDep, StrictModel
from app.db.models import MainCashAccount, MainCashAccountAuditRun, MainCashAccountLog
from app.features.main_cash_account_events import pending_summary
from app.features.main_cash_account_service import (
    ROW_SOURCES,
    adjust,
    get_account,
    main_cash_account,
    parse_reference,
)

router = APIRouter(tags=["Main Cash Account"])


class AdjustmentInput(StrictModel):
    direction: Literal["credit", "debit"]
    amount: Decimal = Field(gt=0, description="Whole IDR; credit adds to and debit subtracts from the account.")
    note: str = Field(min_length=1, max_length=1000, description="Stored in the log reference as 'manual:<note>'.")


def account_out(db: DbSession, account: MainCashAccount) -> dict[str, object]:
    pending_count, pending_amount = pending_summary(db)
    return {
        "id": account.id,
        "current_amount": decimal_string(account.current_amount, 0),
        "updated_at": account.updated_at.isoformat(),
        # Committed top-ups/transfers not posted by the background worker yet (normally 0 within a
        # moment). current_amount + pending_amount is the amount once the queue is drained.
        "pending_entries": pending_count,
        "pending_amount": decimal_string(pending_amount, 0),
    }


def log_out(entry: MainCashAccountLog) -> dict[str, object]:
    return {
        "id": entry.id,
        "main_cash_account_id": entry.main_cash_account_id,
        "amount": decimal_string(entry.amount, 0),
        "direction": entry.direction,
        "reference": entry.reference,
        "source": parse_reference(entry.reference),
        "created_at": entry.created_at.isoformat(),
    }


def local_day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=settings.timezone)


@router.get("/main-cash-account")
def get_main_cash_account(_: ActiveUser, db: DbSession) -> dict[str, object]:
    return {"data": account_out(db, main_cash_account(db))}


@router.get("/main-cash-account/logs")
def list_main_cash_account_logs(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    direction: Literal["credit", "debit"] | None = None,
    source: Literal["operational_cashflows", "revenues", "loan_repayments", "manual"] | None = None,
    source_id: int | None = Query(
        default=None, description="Only with source=operational_cashflows, revenues or loan_repayments."
    ),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    if source_id is not None and source not in ROW_SOURCES:
        raise validation(
            "source_id", "source_id requires source=operational_cashflows, source=revenues or source=loan_repayments."
        )
    stmt = select(MainCashAccountLog)
    if direction:
        stmt = stmt.where(MainCashAccountLog.direction == direction)
    if source:
        prefix = f"{source}:{source_id}:" if source_id is not None else f"{source}:"
        stmt = stmt.where(func.starts_with(MainCashAccountLog.reference, prefix))
    if from_date:
        stmt = stmt.where(MainCashAccountLog.created_at >= local_day_start(from_date))
    if to_date:
        stmt = stmt.where(MainCashAccountLog.created_at < local_day_start(to_date + timedelta(days=1)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(MainCashAccountLog.created_at.desc(), MainCashAccountLog.id.desc())
        .offset(page.offset)
        .limit(page.per_page)
    ).all()
    return {"data": [log_out(row) for row in rows], "meta": page.meta(total)}


@router.get("/main-cash-account/logs/{log_id}")
def get_main_cash_account_log(log_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    entry = db.get(MainCashAccountLog, log_id)
    if not entry:
        raise not_found("Main cash account log")
    return {"data": log_out(entry)}


def audit_run_out(run: MainCashAccountAuditRun) -> dict[str, object]:
    return {
        "id": run.id,
        "mode": run.mode,
        "status": run.status,
        "dry_run": run.dry_run,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "window_from": run.window_from.isoformat() if run.window_from else None,
        "sources_checked": run.sources_checked,
        "batches": run.batches,
        "discrepancies": run.discrepancies,
        "entries_posted": run.entries_posted,
        "events_processed": run.events_processed,
        "credit_posted": decimal_string(run.credit_posted, 0),
        "debit_posted": decimal_string(run.debit_posted, 0),
        "previous_amount": decimal_string(run.previous_amount, 0),
        "new_amount": decimal_string(run.new_amount, 0),
        "error": run.error,
    }


@router.get("/main-cash-account/audit-runs")
def list_main_cash_account_audit_runs(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    status_filter: Literal["RUNNING", "SUCCESS", "FAILED", "SKIPPED"] | None = Query(default=None, alias="status"),
    mode: Literal["full", "incremental"] | None = None,
) -> dict[str, object]:
    """History of the daily audit background job (app/scripts/audit_main_cash_account.py)."""
    stmt = select(MainCashAccountAuditRun)
    if status_filter:
        stmt = stmt.where(MainCashAccountAuditRun.status == status_filter)
    if mode:
        stmt = stmt.where(MainCashAccountAuditRun.mode == mode)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(MainCashAccountAuditRun.started_at.desc(), MainCashAccountAuditRun.id.desc())
        .offset(page.offset)
        .limit(page.per_page)
    ).all()
    return {"data": [audit_run_out(row) for row in rows], "meta": page.meta(total)}


@router.post("/main-cash-account/adjustments", status_code=status.HTTP_201_CREATED)
def create_main_cash_account_adjustment(
    payload: AdjustmentInput, actor: Superadmin, db: DbSession
) -> dict[str, object]:
    amount = money(payload.amount)
    if amount <= 0:
        raise validation("amount", "Amount must be at least 1 IDR.")
    entry = adjust(db, payload.direction, amount, payload.note, actor)
    db.commit()
    account = get_account(db)
    assert account is not None
    return {"data": {"log": log_out(entry), "account": account_out(db, account)}}
