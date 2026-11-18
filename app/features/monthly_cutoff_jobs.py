"""Background jobs of the monthly cut-off (``monthly_cutoff_jobs``).

Creating a cut-off (sync the month's cash statement, compute every figure, then generate the
Excel export) and generating an export are heavy, so the API never runs them: it only queues a
job and answers ``202``.

- ``POST /monthly-cutoffs/jobs`` queues a ``CUTOFF`` (create or recreate the month's cut-off, then
  its export) or an ``EXPORT`` (the Excel file of the month's reported cut-off) for a user;
  ``GET /monthly-cutoffs/jobs/{id}`` tells where it stands. The list
  (``GET /monthly-cutoffs/months``) shows the status too, so the user just refreshes it.
- The worker (``python -m app.scripts.monthly_cutoff_worker``) runs the queued jobs, oldest
  first; several workers can run at once (``FOR UPDATE SKIP LOCKED``).
- The hourly cron (``monthly_cutoff --pending``) closes months through jobs too (automatic:
  ``requested_by`` NULL): it takes over a queued job of the month if there is one, or starts a new
  one, and finally runs whatever is still queued. So at most one cut-off per month runs at a time
  (a partial unique index), whoever asked, every attempt is visible with its outcome (e.g. waiting
  for operational data), and queued jobs still run, within the hour, if no worker is deployed.

A job whose worker dies stays ``RUNNING``; after ``CUTOFF_JOB_TIMEOUT_MINUTES`` it is marked
``FAILED``/``INTERRUPTED`` and the month can be queued again. The hourly attempts that only found
operational's data not ready yet are not piled up: the latest one is kept.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Literal

from fastapi import APIRouter, status
from pydantic import Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.dependencies import DbSession, FinanceReader
from app.core.errors import AppError, conflict, not_found, validation
from app.core.pagination import StrictModel
from app.db.models import MonthlyCutoffExport, MonthlyCutoffJob
from app.db.session import SessionLocal
from app.features.cash_statement_service import Period
from app.features.common import now_utc
from app.features.month_end_assets import MonthEndDataNotReadyError
from app.features.monthly_cutoff_export import (
    CutoffNotFoundError,
    ExportGenerationError,
    ExportInProgressError,
    ExportOutcome,
    generate_export,
    latest_cutoff,
)
from app.features.monthly_cutoff_export_state import GENERATING as EXPORT_GENERATING
from app.features.monthly_cutoff_export_state import export_state
from app.features.monthly_cutoff_job_state import (
    ACTIVE,
    ALREADY_RUNNING,
    CUTOFF_JOB,
    ERROR,
    EXPORT_JOB,
    FAILED,
    INTERRUPTED,
    NOT_READY,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    interrupted_reason,
    job_out,
    job_timeout,
    user_names,
)
from app.features.monthly_cutoff_service import CutoffOutcome, run_cutoff
from app.features.monthly_cutoffs import MAX_YEAR, MIN_YEAR

logger = logging.getLogger(__name__)

MAX_REASON = 1000
DATASET_LABELS = {"egg-stock": "egg stock", "fish-weight-estimate": "fish weight estimate"}


class JobActiveError(RuntimeError):
    """A job of this type is already queued or running for the month."""

    def __init__(self, job: MonthlyCutoffJob) -> None:
        super().__init__(f"a {job.job_type} job of {job.period_start:%Y-%m} is already {job.status.lower()}")
        self.job = job


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    if session is not None:
        yield session
    else:
        with SessionLocal() as owned:
            yield owned


# --- Queue --------------------------------------------------------------------------------------


def reap_interrupted(db: Session) -> int:
    """Marks the jobs running for longer than the timeout (their worker died) as interrupted."""
    now = now_utc()
    result = db.execute(
        update(MonthlyCutoffJob)
        .where(MonthlyCutoffJob.status == RUNNING, MonthlyCutoffJob.started_at < now - job_timeout())
        .values(status=FAILED, reason_code=INTERRUPTED, reason=interrupted_reason(), finished_at=now)
    )
    db.commit()
    return int(getattr(result, "rowcount", 0) or 0)


def active_job(db: Session, job_type: str, period: Period) -> MonthlyCutoffJob | None:
    return db.scalar(
        select(MonthlyCutoffJob).where(
            MonthlyCutoffJob.job_type == job_type,
            MonthlyCutoffJob.period_start == period.start,
            MonthlyCutoffJob.status.in_(ACTIVE),
        )
    )


def _insert(db: Session, job_type: str, period: Period, status: str, requested_by: int | None) -> int | None:
    """A new job, unless one of this type is already active for the month (then ``None``)."""
    now = now_utc()
    table = MonthlyCutoffJob.__table__
    statement = (
        insert(MonthlyCutoffJob)
        .values(
            job_type=job_type,
            period_start=period.start,
            status=status,
            requested_by=requested_by,
            requested_at=now,
            started_at=now if status == RUNNING else None,
        )
        .on_conflict_do_nothing(
            index_elements=[table.c.job_type, table.c.period_start], index_where=table.c.status.in_(ACTIVE)
        )
        .returning(table.c.id)
    )
    job_id = db.execute(statement).scalar_one_or_none()
    db.commit()
    return job_id


def enqueue(db: Session, job_type: str, period: Period, requested_by: int | None) -> MonthlyCutoffJob:
    """Queues a job for the worker; :class:`JobActiveError` if one is already queued or running."""
    reap_interrupted(db)
    job_id = _insert(db, job_type, period, QUEUED, requested_by)
    if job_id is None:
        existing = active_job(db, job_type, period)
        if existing is None:  # finished in between: try once more
            job_id = _insert(db, job_type, period, QUEUED, requested_by)
        else:
            raise JobActiveError(existing)
    if job_id is None:
        raise RuntimeError(f"could not queue the {job_type} job of {period.label}")
    job = db.get(MonthlyCutoffJob, job_id)
    assert job is not None
    return job


def claim_next(db: Session) -> int | None:
    """Takes the oldest queued job (``RUNNING``, committed); ``None`` if there is none."""
    job = db.scalar(
        select(MonthlyCutoffJob)
        .where(MonthlyCutoffJob.status == QUEUED)
        .order_by(MonthlyCutoffJob.requested_at, MonthlyCutoffJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        db.rollback()
        return None
    job.status, job.started_at = RUNNING, now_utc()
    job_id = job.id
    db.commit()
    return job_id


def start_automatic(db: Session, job_type: str, period: Period) -> int | None:
    """For the cron: takes over the month's queued job of this type if there is one, else starts
    a new one (``RUNNING``). ``None`` if one is running already."""
    reap_interrupted(db)
    queued = db.scalar(
        select(MonthlyCutoffJob)
        .where(
            MonthlyCutoffJob.job_type == job_type,
            MonthlyCutoffJob.period_start == period.start,
            MonthlyCutoffJob.status == QUEUED,
        )
        .with_for_update(skip_locked=True)
    )
    if queued is not None:
        queued.status, queued.started_at = RUNNING, now_utc()
        job_id = queued.id
        db.commit()
        return job_id
    db.rollback()
    return _insert(db, job_type, period, RUNNING, None)


# --- Running ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobResult:
    job_id: int
    job_type: str
    period: Period
    status: str
    reason_code: str | None = None
    reason: str | None = None
    cutoff: CutoffOutcome | None = None  # a CUTOFF that succeeded
    not_ready: MonthEndDataNotReadyError | None = None
    export: ExportOutcome | None = None  # the export generated (after a CUTOFF, or by an EXPORT)
    export_error: str | None = None  # why the export after a successful CUTOFF failed


def not_ready_reason(waiting: MonthEndDataNotReadyError) -> str:
    missing = ", ".join(DATASET_LABELS.get(item.path.rsplit("/", 1)[-1], item.path) for item in waiting.missing)
    return (
        f"operational-service has not published the month-end data of {waiting.period.label} yet ({missing}); "
        "the cut-off is created once it has."
    )


def finish(
    db: Session,
    job_id: int,
    status: str,
    reason_code: str | None = None,
    reason: str | None = None,
    cutoff_id: int | None = None,
) -> None:
    db.rollback()
    db.execute(
        update(MonthlyCutoffJob)
        .where(MonthlyCutoffJob.id == job_id)
        .values(
            status=status,
            reason_code=reason_code,
            reason=reason[:MAX_REASON] if reason else None,
            cutoff_id=cutoff_id,
            finished_at=now_utc(),
        )
    )
    db.commit()


def prune_waiting(db: Session, job_id: int, period: Period) -> None:
    """Keeps only the latest of the hourly (automatic) attempts that found the data not ready."""
    db.execute(
        delete(MonthlyCutoffJob).where(
            MonthlyCutoffJob.job_type == CUTOFF_JOB,
            MonthlyCutoffJob.period_start == period.start,
            MonthlyCutoffJob.requested_by.is_(None),
            MonthlyCutoffJob.reason_code == NOT_READY,
            MonthlyCutoffJob.id != job_id,
        )
    )
    db.commit()


def _run_cutoff(db: Session, job_id: int, period: Period, requested_by: int | None) -> JobResult:
    try:
        outcome = run_cutoff(period=period, session=db)
    except MonthEndDataNotReadyError as waiting:
        reason = not_ready_reason(waiting)
        finish(db, job_id, FAILED, NOT_READY, reason)
        if requested_by is None:
            prune_waiting(db, job_id, period)
        return JobResult(job_id, CUTOFF_JOB, period, FAILED, NOT_READY, reason, not_ready=waiting)
    except Exception as exc:
        logger.exception("monthly cut-off job %s (%s) failed", job_id, period.label)
        reason = f"{type(exc).__name__}: {exc}"
        finish(db, job_id, FAILED, ERROR, reason)
        return JobResult(job_id, CUTOFF_JOB, period, FAILED, ERROR, reason[:MAX_REASON])
    finish(db, job_id, SUCCEEDED, cutoff_id=outcome.cutoff_id)
    # The new cut-off's Excel file; its own status says how it went (a failure does not undo the close).
    export, export_error = None, None
    try:
        export = generate_export(period, requested_by=requested_by, session=db)
    except ExportGenerationError as exc:
        export_error = exc.reason
    except (ExportInProgressError, CutoffNotFoundError) as exc:
        export_error = str(exc)
    return JobResult(
        job_id, CUTOFF_JOB, period, SUCCEEDED, cutoff=outcome, export=export, export_error=export_error
    )


def _run_export(db: Session, job_id: int, period: Period, requested_by: int | None) -> JobResult:
    try:
        exported = generate_export(period, requested_by=requested_by, session=db)
    except CutoffNotFoundError as exc:
        reason_code, reason = ERROR, str(exc)
    except ExportInProgressError as exc:
        reason_code, reason = ALREADY_RUNNING, str(exc)
    except ExportGenerationError as exc:
        reason_code, reason = ERROR, exc.reason
    except Exception as exc:
        logger.exception("monthly cut-off export job %s (%s) failed", job_id, period.label)
        reason_code, reason = ERROR, f"{type(exc).__name__}: {exc}"
    else:
        finish(db, job_id, SUCCEEDED, cutoff_id=exported.cutoff_id)
        return JobResult(job_id, EXPORT_JOB, period, SUCCEEDED, export=exported)
    finish(db, job_id, FAILED, reason_code, reason)
    return JobResult(job_id, EXPORT_JOB, period, FAILED, reason_code, reason[:MAX_REASON])


def execute(job_id: int, session: Session | None = None) -> JobResult:
    """Runs a job that has been claimed (``RUNNING``) and records its outcome."""
    with _session(session) as db:
        job = db.get(MonthlyCutoffJob, job_id)
        if job is None:
            raise LookupError(f"monthly cut-off job {job_id} does not exist")
        job_type, period, requested_by = job.job_type, Period(job.period_start), job.requested_by
        logger.info("monthly cut-off job %s: %s of %s started", job_id, job_type, period.label)
        run = _run_cutoff if job_type == CUTOFF_JOB else _run_export
        result = run(db, job_id, period, requested_by)
    logger.info(
        "monthly cut-off job %s: %s of %s %s%s",
        job_id,
        job_type,
        period.label,
        result.status.lower(),
        f" ({result.reason_code}: {result.reason})" if result.reason else "",
    )
    return result


def run_queued(session: Session | None = None, limit: int | None = None) -> list[JobResult]:
    """Runs the queued jobs one after the other, oldest first, until none is left (or ``limit``)."""
    results: list[JobResult] = []
    with _session(session) as db:
        reap_interrupted(db)
        while limit is None or len(results) < limit:
            job_id = claim_next(db)
            if job_id is None:
                break
            results.append(execute(job_id, session=db))
    return results


# --- API ----------------------------------------------------------------------------------------

router = APIRouter(tags=["Monthly Cut-off"])


class JobRequest(StrictModel):
    type: Literal["CUTOFF", "EXPORT"] = Field(
        description="CUTOFF: create (or recreate) the month's cut-off, then its Excel file. "
        "EXPORT: (re)generate the Excel file of the month's cut-off."
    )
    year: int = Field(ge=MIN_YEAR, le=MAX_YEAR)
    month: int = Field(ge=1, le=12)


def job_response(db: Session, job: MonthlyCutoffJob) -> dict[str, object]:
    return job_out(job, user_names(db, [job.requested_by]), now_utc())


@router.post(
    "/monthly-cutoffs/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"description": "EXPORT of a month that has not been closed."},
        409: {"description": "A job of this type is already queued or running for the month."},
        422: {"description": "The month has not ended."},
    },
)
def queue_monthly_cutoff_job(payload: JobRequest, actor: FinanceReader, db: DbSession) -> dict[str, object]:
    """Queues the creation (or recreation) of a month's cut-off, or of its Excel file, to run in
    the background, and answers at once (``202``) with the queued job. The user refreshes the list
    (``GET /monthly-cutoffs/months``) to see it running, then its outcome."""
    period = Period(date(payload.year, payload.month, 1))
    now = now_utc()
    if period.ends_at > now:
        raise validation("month", f"{period.label} has not ended yet: its cut-off cannot be created.")
    if payload.type == EXPORT_JOB:
        cutoff = latest_cutoff(db, period)
        if cutoff is None:
            raise AppError(
                404, "NOT_FOUND", f"The monthly cut-off of {period.label} was not found: the month has not been closed."
            )
        export = db.scalar(select(MonthlyCutoffExport).where(MonthlyCutoffExport.cutoff_id == cutoff.id))
        if export_state(export, now).status == EXPORT_GENERATING:
            raise conflict("JOB_IN_PROGRESS", f"The Excel file of {period.label} is being generated already.")
    try:
        job = enqueue(db, payload.type, period, actor.id)
    except JobActiveError as exc:
        what = "cut-off" if payload.type == CUTOFF_JOB else "Excel file"
        raise conflict(
            "JOB_IN_PROGRESS",
            f"The {what} of {period.label} is already {exc.job.status.lower()}.",
            job=job_response(db, exc.job),
        ) from exc
    return {"data": job_response(db, job)}


@router.get("/monthly-cutoffs/jobs/{job_id}")
def get_monthly_cutoff_job(job_id: int, _: FinanceReader, db: DbSession) -> dict[str, object]:
    """One background job: ``QUEUED``, ``RUNNING``, ``SUCCEEDED`` or ``FAILED`` (``reason_code``,
    ``reason``)."""
    job = db.get(MonthlyCutoffJob, job_id)
    if job is None:
        raise not_found("Monthly cut-off job")
    return {"data": job_response(db, job)}
