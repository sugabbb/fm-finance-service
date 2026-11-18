"""Excel export of the monthly cut-offs, for the finance team.

Every successful close gets a workbook: it is generated right after the close (by the cut-off job,
see ``monthly_cutoff_jobs``); the hourly ``monthly_cutoff --pending`` run generates the ones not
started yet and retries the failed ones, up to ``EXPORT_MAX_AUTOMATIC_ATTEMPTS`` failures in a row;
a user can queue one from the frontend (``POST /monthly-cutoffs/jobs``, type ``EXPORT``) at any
time, e.g. after the automatic retries gave up; and ``app.scripts.export_monthly_cutoff``
regenerates one by hand. Every attempt is recorded in
``monthly_cutoff_exports`` (status, failures, reason; see ``monthly_cutoff_export_state``), and at
most one runs at a time per cut-off.

The workbook holds the same data as the API (``GET /monthly-cutoffs`` and the month's
``cash_statements``), laid out by ``monthly_cutoff_workbook``, and is kept in the export storage
(``app.integrations.export_storage``: the local disk or Google Cloud Storage, per
``EXPORT_STORAGE_BACKEND``) under a key per cut-off, so a restated
month has a new workbook and the earlier ones are kept.

The cut-off is a snapshot but the cash statement is not: the rows are read when the workbook is
generated (under the balances lock, so they are a consistent picture) and checked against the
snapshot. If they no longer match it (entries changed after the close), the workbook says so;
closing the month again gives figures that match.

Endpoint: ``GET /api/v1/monthly-cutoffs/export?year=&month=`` downloads the workbook of the month's
reported cut-off. The path says "export" on purpose: reports are operational-service's (see
test_openapi).
"""

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.dependencies import DbSession, FinanceReader
from app.core.errors import AppError
from app.db.models import FinancialOpeningBalance, MonthlyCutoff, MonthlyCutoffExport, MonthlyCutoffJob
from app.db.session import SessionLocal
from app.features.cash_statement_service import Period
from app.features.cash_statements import statement_out, statement_query
from app.features.common import now_utc
from app.features.locks import lock_balances
from app.features.monthly_cutoff_export_state import (
    FAILED,
    GENERATING,
    SUCCEEDED,
    export_out,
    export_state,
    generation_timeout,
    requester_names,
)
from app.features.monthly_cutoff_job_state import ACTIVE, EXPORT_JOB
from app.features.monthly_cutoff_workbook import StatementLine, WorkbookData, build_workbook
from app.features.monthly_cutoffs import MAX_YEAR, MIN_YEAR, cutoff_out, single_value
from app.integrations.export_storage import ExportFileMissing, ExportStorage, ExportStorageError, export_storage

logger = logging.getLogger(__name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OPENING_BALANCE = "OPENING_BALANCE"
ACCOUNT_LABELS = {"OPERATIONAL": "operational account", "REVENUE": "revenue account"}
# How a statement row's transaction is called in the finance application ("revenues:12" -> "Revenue #12").
REFERENCE_LABELS = {
    "revenues": "Revenue",
    "operational_cashflows": "Operational cashflow",
    "main_cash_account_logs": "Main account log",
    "loans": "Loan",
}
# Sources whose transaction date is the date of a sale/expense (a loan's is its start date).
DATED_SOURCES = ("revenues", "operational_cashflows")


def reference_label(source_table: str, source_id: object) -> str:
    return f"{REFERENCE_LABELS.get(source_table, source_table)} #{source_id}"


class CutoffNotFoundError(LookupError):
    """The month has no reported cut-off to export."""


class ExportInProgressError(RuntimeError):
    """The cut-off's export is being generated already."""


class ExportGenerationError(RuntimeError):
    """The generation failed; ``reason`` is what was recorded (and shown to users)."""

    def __init__(self, period: Period, reason: str) -> None:
        super().__init__(f"the Excel export of {period.label} failed: {reason}")
        self.period = period
        self.reason = reason


def file_name(period: Period) -> str:
    return f"monthly-cutoff-{period.label}.xlsx"


def storage_key(period: Period, cutoff_id: int) -> str:
    return f"monthly-cutoffs/{period.label}/cutoff-{cutoff_id}.xlsx"


def latest_cutoff(db: Session, period: Period) -> MonthlyCutoff | None:
    return db.scalar(
        select(MonthlyCutoff).where(MonthlyCutoff.period_start == period.start, MonthlyCutoff.is_latest.is_(True))
    )


# --- Data ---------------------------------------------------------------------------------------


def statement_lines(db: Session, period: Period, opening: Decimal) -> list[StatementLine]:
    """The month's cash statement (as the API returns it), oldest first, plus the go-live opening
    balances effective during the month, with the cash balance running from ``opening``."""
    statement = statement_query()
    rows = [
        statement_out(row)
        for row in db.execute(
            select(statement)
            .where(statement.c.statement_date.between(period.start, period.end))
            .order_by(statement.c.statement_date, statement.c.id)
        ).all()
    ]
    openings = db.scalars(
        select(FinancialOpeningBalance)
        .where(FinancialOpeningBalance.effective_date.between(period.start, period.end))
        .order_by(FinancialOpeningBalance.effective_date, FinancialOpeningBalance.id)
    ).all()
    # (day, order within the day, line without its balance): opening balances first on their day.
    events: list[tuple[date, int, StatementLine]] = [
        (
            item.effective_date,
            0,
            StatementLine(
                day=item.effective_date,
                description=f"Go-live opening balance, {ACCOUNT_LABELS.get(item.account, item.account.lower())}",
                category=OPENING_BALANCE,
                source_table=None,
                counterparty=None,
                reference=None,
                transaction_date=None,
                money_in=Decimal(item.amount_idr),
                money_out=None,
                balance=Decimal("0"),
            ),
        )
        for item in openings
    ]
    for row in rows:
        day = date.fromisoformat(str(row["statement_date"]))
        source: dict[str, Any] = row["source"]  # type: ignore[assignment]
        dated = source["table"] in DATED_SOURCES and source["transaction_date"]
        transaction_date = date.fromisoformat(source["transaction_date"]) if dated else None
        events.append(
            (
                day,
                1,
                StatementLine(
                    day=day,
                    description=str(row["description"]),
                    category=str(row["category"]),
                    source_table=str(source["table"]),
                    counterparty=row["counterparty"] if isinstance(row["counterparty"], str) else None,
                    reference=reference_label(source["table"], source["id"]),
                    transaction_date=transaction_date if transaction_date != day else None,
                    money_in=Decimal(str(row["credit"])) if row["credit"] is not None else None,
                    money_out=Decimal(str(row["debit"])) if row["debit"] is not None else None,
                    balance=Decimal("0"),
                ),
            )
        )
    events.sort(key=lambda event: (event[0], event[1]))  # stable: statement order kept within a day
    balance = opening
    lines: list[StatementLine] = []
    for _, _, line in events:
        balance += (line.money_in or 0) - (line.money_out or 0)
        lines.append(replace(line, balance=balance))
    return lines


def statement_warnings(cutoff: dict[str, Any], lines: list[StatementLine], period: Period) -> list[str]:
    """A warning when the statement read now is not the one the cut-off was computed from."""
    cash = cutoff["cash"]
    entries = [line for line in lines if line.category != OPENING_BALANCE]
    now = (
        len(entries),
        sum((line.money_in or 0 for line in entries), Decimal("0")),
        sum((line.money_out or 0 for line in entries), Decimal("0")),
        sum((line.money_in or 0 for line in lines if line.category == OPENING_BALANCE), Decimal("0")),
    )
    closed = (
        int(cash["statement_rows"]),
        Decimal(cash["cash_in"]),
        Decimal(cash["cash_out"]),
        Decimal(cash["opening_balances_in_period"]),
    )
    if now == closed:
        return []
    return [
        f"The cash statement of {period.label} has changed since this cut-off was taken: now {now[0]} entries, "
        f"money in {now[1]:,.0f}, money out {now[2]:,.0f}; at the close {closed[0]} entries, money in "
        f"{closed[1]:,.0f}, money out {closed[2]:,.0f}. The Cash Statement sheet shows the entries as they are "
        f"now, the other sheets the close. Close the month again for figures that match."
    ]


def workbook_data(db: Session, cutoff: MonthlyCutoff) -> WorkbookData:
    period = Period(cutoff.period_start)
    payload: dict[str, Any] = cutoff_out(db, cutoff)
    lines = statement_lines(db, period, Decimal(payload["cash"]["opening"]))
    return WorkbookData(
        cutoff=payload, statement=lines, generated_at=now_utc(), warnings=statement_warnings(payload, lines, period)
    )


# --- Export -------------------------------------------------------------------------------------

MAX_REASON = 1000


@dataclass(frozen=True)
class ExportOutcome:
    cutoff_id: int
    period: Period
    storage_key: str
    file_name: str
    size_bytes: int
    warnings: list[str]


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    if session is not None:
        yield session
    else:
        with SessionLocal() as owned:
            yield owned


def claim(db: Session, cutoff_id: int, requested_by: int | None) -> bool:
    """Marks the cut-off's export ``GENERATING`` (committed) unless another generation is running;
    ``False`` if one is. An interrupted generation (running too long) is taken over and counted as
    a failed attempt."""
    now = now_utc()
    table = MonthlyCutoffExport.__table__
    attempt = {"status": GENERATING, "attempt_started_at": now, "attempt_finished_at": None}
    statement = (
        insert(MonthlyCutoffExport)
        .values(cutoff_id=cutoff_id, failed_attempts=0, warnings=[], attempt_requested_by=requested_by, **attempt)
        .on_conflict_do_update(
            index_elements=[table.c.cutoff_id],
            set_={
                **attempt,
                "attempt_requested_by": requested_by,
                "failed_attempts": case(
                    (table.c.status == GENERATING, table.c.failed_attempts + 1), else_=table.c.failed_attempts
                ),
            },
            where=or_(table.c.status != GENERATING, table.c.attempt_started_at < now - generation_timeout()),
        )
        .returning(table.c.id)
    )
    claimed = db.execute(statement).scalar_one_or_none() is not None
    db.commit()
    return claimed


def record_failure(db: Session, cutoff_id: int, reason: str) -> None:
    try:
        db.rollback()
        db.execute(
            update(MonthlyCutoffExport)
            .where(MonthlyCutoffExport.cutoff_id == cutoff_id)
            .values(
                status=FAILED,
                failed_attempts=MonthlyCutoffExport.failed_attempts + 1,
                last_error=reason,
                attempt_finished_at=now_utc(),
            )
        )
        db.commit()
    except Exception:  # it stays GENERATING and counts as interrupted after the timeout
        logger.exception("could not record the failed export of cut-off %s", cutoff_id)


def generate_export(
    period: Period,
    *,
    requested_by: int | None = None,
    session: Session | None = None,
    storage: ExportStorage | None = None,
) -> ExportOutcome:
    """Generates (or regenerates) the workbook of ``period``'s reported cut-off, stores it and
    records the attempt. ``requested_by``: the user who asked from the frontend (``None``: an
    automatic or command-line attempt).

    Raises :class:`CutoffNotFoundError` if the month has not been closed,
    :class:`ExportInProgressError` if it is being generated already, and
    :class:`ExportGenerationError` (recorded as ``FAILED``) if the generation fails."""
    store = storage or export_storage()
    with _session(session) as db:
        cutoff = latest_cutoff(db, period)
        if cutoff is None:
            raise CutoffNotFoundError(f"{period.label} has no monthly cut-off to export: the month has not been closed")
        cutoff_id = cutoff.id
        if not claim(db, cutoff_id, requested_by):
            raise ExportInProgressError(f"the Excel export of {period.label} is being generated already")
        try:
            try:
                lock_balances(db)  # the snapshot and the statement rows, as one consistent picture
                data = workbook_data(db, cutoff)
            finally:
                db.rollback()  # releases the lock before the (slower) workbook is built
            content = build_workbook(data)
            location = store.save(storage_key(period, cutoff_id), content, XLSX)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"[:MAX_REASON]
            record_failure(db, cutoff_id, reason)
            raise ExportGenerationError(period, reason) from exc
        db.execute(
            update(MonthlyCutoffExport)
            .where(MonthlyCutoffExport.cutoff_id == cutoff_id)
            .values(
                status=SUCCEEDED,
                failed_attempts=0,
                last_error=None,
                attempt_finished_at=now_utc(),
                storage_backend=store.backend,
                storage_key=location,
                file_name=file_name(period),
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                generated_at=data.generated_at,
                warnings=list(data.warnings),
            )
        )
        db.commit()
    for warning in data.warnings:
        logger.warning("monthly cut-off export %s (cut-off %s): %s", period.label, cutoff_id, warning)
    return ExportOutcome(cutoff_id, period, location, file_name(period), len(content), list(data.warnings))


@dataclass(frozen=True)
class RetryResult:
    period: Period
    outcome: ExportOutcome | None
    error: str | None  # None: succeeded


def latest_export_job(db: Session, cutoff: MonthlyCutoff) -> MonthlyCutoffJob | None:
    return db.scalar(
        select(MonthlyCutoffJob)
        .where(MonthlyCutoffJob.job_type == EXPORT_JOB, MonthlyCutoffJob.period_start == cutoff.period_start)
        .order_by(MonthlyCutoffJob.requested_at.desc(), MonthlyCutoffJob.id.desc())
        .limit(1)
    )


def due_for_automatic_attempt(db: Session) -> list[Period]:
    """The months whose reported cut-off's export should be generated automatically: not started
    yet, or failed fewer than ``EXPORT_MAX_AUTOMATIC_ATTEMPTS`` times in a row, and not queued by a
    user already. Newest first."""
    now = now_utc()
    queued = (
        select(MonthlyCutoffJob.id)
        .where(
            MonthlyCutoffJob.job_type == EXPORT_JOB,
            MonthlyCutoffJob.period_start == MonthlyCutoff.period_start,
            MonthlyCutoffJob.status.in_(ACTIVE),
        )
        .exists()
    )
    rows = db.execute(
        select(MonthlyCutoff.period_start, MonthlyCutoffExport)
        .outerjoin(MonthlyCutoffExport, MonthlyCutoffExport.cutoff_id == MonthlyCutoff.id)
        .where(MonthlyCutoff.is_latest.is_(True), ~queued)
        .order_by(MonthlyCutoff.period_start.desc())
    ).all()
    return [Period(start) for start, row in rows if export_state(row, now).needs_automatic_attempt]


def retry_exports(session: Session | None = None) -> list[RetryResult]:
    """Generates every export due for an automatic attempt (see :func:`due_for_automatic_attempt`),
    one after the other; a failure is recorded and does not stop the others."""
    with _session(session) as db:
        due = due_for_automatic_attempt(db)
        results: list[RetryResult] = []
        for period in due:
            try:
                results.append(RetryResult(period, generate_export(period, session=db), None))
            except (ExportGenerationError, ExportInProgressError, CutoffNotFoundError) as error:
                results.append(RetryResult(period, None, str(error)))
        return results


# --- API ----------------------------------------------------------------------------------------

router = APIRouter(tags=["Monthly Cut-off"])


def reported_cutoff(db: Session, request: Request, year: int, month: int) -> tuple[Period, MonthlyCutoff]:
    single_value(request, "year", "month")
    period = Period(date(year, month, 1))
    cutoff = latest_cutoff(db, period)
    if cutoff is None:
        raise AppError(
            404, "NOT_FOUND", f"The monthly cut-off of {period.label} was not found: the month has not been closed."
        )
    return period, cutoff


def current_export(db: Session, cutoff: MonthlyCutoff) -> dict[str, object]:
    row = db.scalar(select(MonthlyCutoffExport).where(MonthlyCutoffExport.cutoff_id == cutoff.id))
    return export_out(cutoff, row, now_utc(), requester_names(db, [row]), latest_export_job(db, cutoff))


YEAR = Query(ge=MIN_YEAR, le=MAX_YEAR, description="Required, e.g. 2026.")
MONTH = Query(ge=1, le=12, description="Required, 1-12.")


@router.get(
    "/monthly-cutoffs/export",
    response_class=Response,
    responses={
        200: {"content": {XLSX: {}}, "description": "The workbook (.xlsx), as an attachment."},
        404: {"description": "The month has not been closed, or its workbook is not available (yet)."},
        503: {"description": "The storage of the workbook (e.g. Google Cloud Storage) could not be reached."},
    },
)
def download_monthly_cutoff_export(
    request: Request, _: FinanceReader, db: DbSession, year: int = YEAR, month: int = MONTH
) -> Response:
    """The Excel workbook of one month's close (the reported cut-off): summary, profit and loss,
    cash statement, categories, loans, receivables and potential revenue. Same figures as
    ``GET /monthly-cutoffs``.

    ``404 EXPORT_NOT_AVAILABLE`` when it has not been generated (successfully) yet; the body's
    ``export`` says where it stands (status, reason)."""
    period, cutoff = reported_cutoff(db, request, year, month)
    row = db.scalar(select(MonthlyCutoffExport).where(MonthlyCutoffExport.cutoff_id == cutoff.id))

    def not_available() -> AppError:
        return AppError(
            404,
            "EXPORT_NOT_AVAILABLE",
            f"The Excel export of the monthly cut-off of {period.label} is not available.",
            extra={"export": current_export(db, cutoff)},
        )

    if row is None or row.storage_key is None or row.storage_backend is None:
        raise not_available()
    try:
        content = export_storage(row.storage_backend).load(row.storage_key)
    except ExportFileMissing as exc:
        logger.error("export of cut-off %s is recorded but its file %s is missing", cutoff.id, row.storage_key)
        raise not_available() from exc
    except ExportStorageError as exc:
        logger.exception("export of cut-off %s could not be read from %s", cutoff.id, row.storage_key)
        raise AppError(
            503,
            "EXPORT_STORAGE_UNAVAILABLE",
            "The Excel file could not be read from its storage. Try again later.",
        ) from exc
    return Response(
        content=content,
        media_type=XLSX,
        headers={"Content-Disposition": f'attachment; filename="{row.file_name}"', "Cache-Control": "no-store"},
    )
