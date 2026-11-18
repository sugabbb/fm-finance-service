"""Audit engine for the main cash account, used by the daily background command.

It cross-checks ``main_cash_account_logs`` against its sources in both directions:

- every paid ``MAIN_ACCOUNT_TOPUP`` cashflow, paid ``MAIN_ACCOUNT_TRANSFER`` revenue and non-voided
  loan repayment must be represented in the log by its signed effect (source -> log);
- every logged source entry must match a source row that still justifies it, so a log entry whose
  source was changed, unpaid again or deleted is corrected too (log -> source).

Movements still queued in ``main_cash_account_events`` (committed but not posted by the background
worker yet) count as logged, so the audit never double posts them. A (non dry) run first drains
that queue, which makes it the safety net for events whose background task never ran.

Any difference is fixed by posting one compensating entry with reference
``<source table>:<source id>:<action>`` (``audit`` for this command). Manual adjustments
(``manual:...``) are never touched. Afterwards ``current_amount`` is recalculated.

Designed for tables with millions of rows:

- the comparison runs **inside PostgreSQL** (a FULL OUTER JOIN per batch) and returns only
  mismatching rows, so nothing is loaded into Python except actual discrepancies;
- partial indexes restrict both sources to main account rows, so the other (millions of)
  transactions are never scanned;
- work is split into **keyset batches**, each in its own short transaction. The audit does not take
  the cashflow/revenue write lock at all: every writer commits its source change together with its
  queued event and the worker swaps event -> log atomically, so one comparison statement always
  sees a consistent picture. Only the account row is locked, and only while posting;
- corrections are written with **set-based inserts**, not row by row;
- ``incremental`` mode audits only sources touched since the previous successful run (watermark),
  and recalculates the amount from that run's checkpoint using the fact that the log is
  append-only. ``full`` mode re-checks everything and recomputes the amount from scratch.
"""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, ColumnElement, Integer, bindparam, case, func, insert, literal, select, text, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app.db.models import (
    LoanRepayment,
    MainCashAccount,
    MainCashAccountAuditRun,
    MainCashAccountEvent,
    MainCashAccountLog,
    OperationalCashflow,
    Revenue,
)
from app.db.session import SessionLocal, engine
from app.features.common import now_utc
from app.features.main_cash_account_events import drain, pending_summary
from app.features.main_cash_account_service import (
    CASHFLOW_SOURCE,
    CREDIT,
    DEBIT,
    LOAN_REPAYMENT_SOURCE,
    REVENUE_SOURCE,
    TOPUP_TYPE,
    TRANSFER_TYPE,
    get_account,
    get_or_create_account,
    signed_log_amount,
    source_reference,
)

logger = logging.getLogger(__name__)

# Session level lock that keeps two audit runs from overlapping. Different from the transactional
# lock used by the writes themselves (farm_service.ADVISORY_LOCK).
AUDIT_JOB_LOCK = 20261120

DEFAULT_BATCH_SIZE = 5_000
# An incremental run re-checks a little before the previous run started, so a transaction that was
# still open during that run (its updated_at precedes the commit) cannot slip through.
DEFAULT_OVERLAP = timedelta(days=1)
MAX_POSTED_SAMPLE = 1_000

FULL = "full"
INCREMENTAL = "incremental"


@dataclass(frozen=True)
class SourceSpec:
    """One table that feeds the main cash account."""

    name: str
    id_column: Any
    # The rows of the table that can affect the main account (matched by a partial index where the
    # table is large). Only the type: a row that *stops* qualifying must still be audited.
    scope: ColumnElement[bool]
    updated_column: Any
    effect: ColumnElement[Decimal]


def source_specs() -> tuple[SourceSpec, ...]:
    return (
        SourceSpec(
            name=CASHFLOW_SOURCE,
            id_column=OperationalCashflow.id,
            scope=OperationalCashflow.cashflow_type == TOPUP_TYPE,
            updated_column=OperationalCashflow.updated_at,
            # A paid top-up takes money out of the main account.
            effect=case(
                (OperationalCashflow.payment_status == "PAID", -OperationalCashflow.amount_idr),
                else_=literal(Decimal("0")),
            ),
        ),
        SourceSpec(
            name=REVENUE_SOURCE,
            id_column=Revenue.id,
            scope=Revenue.revenue_type == TRANSFER_TYPE,
            updated_column=Revenue.updated_at,
            # A paid (approved) transfer puts money into the main account.
            effect=case((Revenue.payment_status == "PAID", Revenue.amount_idr), else_=literal(Decimal("0"))),
        ),
        SourceSpec(
            name=LOAN_REPAYMENT_SOURCE,
            id_column=LoanRepayment.id,
            scope=true(),
            updated_column=LoanRepayment.updated_at,
            # A repayment takes money out of the main account until it is voided.
            effect=case((LoanRepayment.voided_at.is_(None), -LoanRepayment.amount), else_=literal(Decimal("0"))),
        ),
    )


@dataclass(frozen=True)
class Discrepancy:
    source: str
    source_id: int
    expected: Decimal
    logged: Decimal

    @property
    def delta(self) -> Decimal:
        return self.expected - self.logged


@dataclass
class AuditResult:
    mode: str
    dry_run: bool
    status: str = "RUNNING"
    run_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    window_from: datetime | None = None
    sources_checked: int = 0
    batches: int = 0
    discrepancies: int = 0
    entries_posted: int = 0
    events_processed: int = 0
    credit_posted: Decimal = Decimal("0")
    debit_posted: Decimal = Decimal("0")
    # Net effect of the corrections, used to project the amount of a dry run.
    posted_delta: Decimal = Decimal("0")
    previous_amount: Decimal | None = None
    new_amount: Decimal | None = None
    log_high_id: int | None = None
    posted: list[Discrepancy] = field(default_factory=list)
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.entries_posted) or self.previous_amount != self.new_amount

    @property
    def duration_seconds(self) -> float:
        if not self.started_at or not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()


# --- Queries ------------------------------------------------------------------------------------


def candidate_ids(db: Session, spec: SourceSpec, after: int, limit: int, window_from: datetime | None) -> list[int]:
    """Next batch of source ids to check, from both sides: rows in the source table and rows the
    log refers to (so entries whose source vanished are audited too). Keyset paginated on id.
    """
    source_side = select(spec.id_column.label("source_id")).where(spec.scope, spec.id_column > after)
    log_side = select(MainCashAccountLog.source_id.label("source_id")).where(
        MainCashAccountLog.source_table == spec.name, MainCashAccountLog.source_id > after
    )
    queue_side = select(MainCashAccountEvent.source_id.label("source_id")).where(
        MainCashAccountEvent.source_table == spec.name, MainCashAccountEvent.source_id > after
    )
    if window_from is not None:
        source_side = source_side.where(spec.updated_column >= window_from)
        log_side = log_side.where(MainCashAccountLog.created_at >= window_from)
    union = source_side.union(log_side, queue_side).subquery()
    return list(db.scalars(select(union.c.source_id).order_by(union.c.source_id).limit(limit)).all())


def compare(db: Session, spec: SourceSpec, ids: Sequence[int]) -> list[Discrepancy]:
    """Compares expected effect against logged (posted + queued) effect for the given source ids,
    in one statement, so it sees a consistent snapshot without locking the writers. Returns only the
    rows that disagree (normally none).
    """
    # One array parameter rather than thousands of expanded IN parameters: ~3x faster to plan and
    # execute for a batch of a few thousand ids.
    batch = bindparam("batch_ids", value=list(ids), type_=ARRAY(BigInteger))
    expected = (
        select(spec.id_column.label("source_id"), spec.effect.label("effect"))
        .where(spec.scope, spec.id_column == func.any(batch))
        .subquery()
    )
    posted = select(MainCashAccountLog.source_id.label("source_id"), signed_log_amount().label("amount")).where(
        MainCashAccountLog.source_table == spec.name, MainCashAccountLog.source_id == func.any(batch)
    )
    # Queued but not yet posted by the background worker: already accounted for, not missing.
    queued = select(
        MainCashAccountEvent.source_id.label("source_id"), MainCashAccountEvent.delta.label("amount")
    ).where(MainCashAccountEvent.source_table == spec.name, MainCashAccountEvent.source_id == func.any(batch))
    movements = posted.union_all(queued).subquery()
    logged = (
        select(movements.c.source_id.label("source_id"), func.sum(movements.c.amount).label("total"))
        .group_by(movements.c.source_id)
        .subquery()
    )
    expected_effect = func.coalesce(expected.c.effect, Decimal("0"))
    logged_total = func.coalesce(logged.c.total, Decimal("0"))
    stmt = (
        select(
            func.coalesce(expected.c.source_id, logged.c.source_id),
            expected_effect,
            logged_total,
        )
        .select_from(expected.outerjoin(logged, expected.c.source_id == logged.c.source_id, full=True))
        .where(expected_effect != logged_total)
        .order_by(func.coalesce(expected.c.source_id, logged.c.source_id))
    )
    return [
        Discrepancy(spec.name, int(source_id), Decimal(expected_value), Decimal(logged_value))
        for source_id, expected_value, logged_value in db.execute(stmt).all()
    ]


def post_corrections(db: Session, account: MainCashAccount, found: Sequence[Discrepancy], action: str) -> Decimal:
    """Writes one compensating entry per discrepancy with a single INSERT and moves the account."""
    rows = [
        {
            "main_cash_account_id": account.id,
            "amount": abs(item.delta),
            "direction": CREDIT if item.delta > 0 else DEBIT,
            "reference": source_reference(item.source, item.source_id, action),
        }
        for item in found
        if item.delta != 0
    ]
    if not rows:
        return Decimal("0")
    db.execute(insert(MainCashAccountLog), rows)
    moved = sum((item.delta for item in found), Decimal("0"))
    account.current_amount = Decimal(account.current_amount) + moved
    db.flush()
    return moved


def latest_successful_run(db: Session) -> MainCashAccountAuditRun | None:
    return db.scalar(
        select(MainCashAccountAuditRun)
        .where(
            MainCashAccountAuditRun.status == "SUCCESS",
            MainCashAccountAuditRun.dry_run.is_(False),
        )
        .order_by(MainCashAccountAuditRun.started_at.desc(), MainCashAccountAuditRun.id.desc())
        .limit(1)
    )


def log_sum(db: Session, after_id: int | None = None) -> Decimal:
    stmt = select(func.coalesce(func.sum(signed_log_amount()), Decimal("0")))
    if after_id is not None:
        stmt = stmt.where(MainCashAccountLog.id > after_id)
    return Decimal(db.scalar(stmt) or 0)


def log_high_id(db: Session) -> int | None:
    return db.scalar(select(func.max(MainCashAccountLog.id)))


# --- Run ----------------------------------------------------------------------------------------


def try_job_lock(connection: Any) -> bool:
    return bool(
        connection.execute(
            text("select pg_try_advisory_lock(:key)").bindparams(bindparam("key", type_=Integer)),
            {"key": AUDIT_JOB_LOCK},
        ).scalar()
    )


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    """Uses the caller's session when given (the backfill command passes one), otherwise opens and
    closes its own."""
    if session is not None:
        yield session
    else:
        with SessionLocal() as owned:
            yield owned


def audit_main_cash_account(
    *,
    mode: str = FULL,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    overlap: timedelta = DEFAULT_OVERLAP,
    window_from: datetime | None = None,
    action: str = "audit",
    session: Session | None = None,
) -> AuditResult:
    """Runs one audit. Safe to run while the API serves traffic: it never takes the cashflow/revenue
    write lock, and locks the account row only for the short moments it posts corrections.
    """
    if mode not in (FULL, INCREMENTAL):
        raise ValueError(f"unknown audit mode: {mode}")
    result = AuditResult(mode=mode, dry_run=dry_run, started_at=now_utc())

    lock_connection = engine.connect()
    try:
        if not try_job_lock(lock_connection):
            logger.warning("another main cash account audit is already running; skipping")
            result.status = "SKIPPED"
            result.finished_at = now_utc()
            return result

        with _session(session) as db:
            try:
                _prepare_window(db, result, mode, window_from, overlap)
                _record_start(db, result)
                if not dry_run:
                    # Safety net: post anything whose background task never ran.
                    result.events_processed = drain(session=db)
                _audit_sources(db, result, batch_size, action)
                _recalculate_amount(db, result, result.mode)
                result.status = "SUCCESS"
            except Exception as exc:  # record the failure, then let the caller see it
                db.rollback()
                result.status = "FAILED"
                result.error = f"{type(exc).__name__}: {exc}"
                _finish(db, result)
                raise
            _finish(db, result)
        return result
    finally:
        lock_connection.exec_driver_sql(f"select pg_advisory_unlock({AUDIT_JOB_LOCK})")
        lock_connection.close()


def _prepare_window(
    db: Session, result: AuditResult, mode: str, window_from: datetime | None, overlap: timedelta
) -> None:
    if mode == FULL:
        return
    if window_from is not None:
        result.window_from = window_from
        return
    previous = latest_successful_run(db)
    if previous is None:
        # Nothing to be incremental from yet: behave like a full sweep.
        logger.info("no previous successful audit run; falling back to a full sweep")
        result.mode = FULL
        return
    result.window_from = previous.started_at - overlap


def _record_start(db: Session, result: AuditResult) -> None:
    account = get_account(db)
    result.previous_amount = Decimal(account.current_amount) if account else None
    run = MainCashAccountAuditRun(
        mode=result.mode,
        status="RUNNING",
        dry_run=result.dry_run,
        started_at=result.started_at,
        window_from=result.window_from,
        previous_amount=result.previous_amount,
    )
    db.add(run)
    db.commit()
    result.run_id = run.id


def _audit_sources(db: Session, result: AuditResult, batch_size: int, action: str) -> None:
    for spec in source_specs():
        after = 0
        while True:
            # One short transaction per batch: compare, fix, commit.
            ids = candidate_ids(db, spec, after, batch_size, result.window_from)
            if not ids:
                db.commit()
                break
            found = compare(db, spec, ids)
            result.sources_checked += len(ids)
            result.batches += 1
            result.discrepancies += len(found)
            if found and not result.dry_run:
                account = get_or_create_account(db)
                post_corrections(db, account, found, action)
            for item in found:
                result.entries_posted += 1
                result.posted_delta += item.delta
                if item.delta > 0:
                    result.credit_posted += item.delta
                else:
                    result.debit_posted += -item.delta
                if len(result.posted) < MAX_POSTED_SAMPLE:
                    result.posted.append(item)
            if result.dry_run:
                db.rollback()
            else:
                db.commit()
            after = ids[-1]


def _recalculate_amount(db: Session, result: AuditResult, mode: str) -> None:
    """Re-derives current_amount from the log. ``full`` sums the whole log; ``incremental`` adds
    only the entries written since the previous successful run, which is exact because log entries
    are append only.

    The shortcut is used only when the run found nothing wrong. Any discrepancy means the log (or
    the account) was edited outside the API, so the append-only assumption no longer holds and the
    amount is recomputed from the whole log.
    """
    # Lock the account row first: every log insert happens under this lock, so the sum below
    # cannot miss an entry committed in between.
    account = None if result.dry_run else get_or_create_account(db)
    use_checkpoint = mode == INCREMENTAL and result.discrepancies == 0
    previous = latest_successful_run(db) if use_checkpoint else None
    if previous is not None and previous.new_amount is not None and previous.log_high_id is not None:
        total = Decimal(previous.new_amount) + log_sum(db, after_id=previous.log_high_id)
    else:
        total = log_sum(db)
    high = log_high_id(db)
    if account is None:
        # Dry run: corrections were rolled back and the queue was not drained, so report what the
        # amount *would* become.
        _, queued = pending_summary(db)
        result.new_amount = total + result.posted_delta + queued
        result.log_high_id = high
        db.rollback()
        return
    account.current_amount = total
    db.flush()
    result.new_amount = Decimal(account.current_amount)
    result.log_high_id = high
    db.commit()


def _finish(db: Session, result: AuditResult) -> None:
    result.finished_at = now_utc()
    if result.run_id is None:
        return
    run = db.get(MainCashAccountAuditRun, result.run_id)
    if run is None:
        return
    run.status = result.status
    run.finished_at = result.finished_at
    run.mode = result.mode
    run.sources_checked = result.sources_checked
    run.batches = result.batches
    run.discrepancies = result.discrepancies
    run.entries_posted = result.entries_posted
    run.events_processed = result.events_processed
    run.credit_posted = result.credit_posted
    run.debit_posted = result.debit_posted
    run.new_amount = result.new_amount
    run.log_high_id = result.log_high_id
    run.error = result.error
    db.commit()
