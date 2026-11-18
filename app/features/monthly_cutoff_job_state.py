"""Where the background jobs of the monthly cut-off stand (``monthly_cutoff_jobs``), and the
status of each month's cut-off as the list shows it. Read-only helpers: running the jobs is
``monthly_cutoff_jobs``.

Cut-off status of a month (``cutoff.status`` in ``GET /monthly-cutoffs/months``):

``NOT_ENDED``
    The month has not ended: it cannot be closed yet.
``NOT_CREATED``
    The month has ended but has no cut-off, and none has been attempted.
``QUEUED`` / ``RUNNING``
    A cut-off of the month is waiting for the worker / being created (also when it recreates an
    existing one: the current figures stay until it succeeds).
``WAITING_FOR_DATA``
    The last attempt could not close the month: operational-service has not published its
    month-end data yet (the hourly run tries again).
``FAILED``
    The last attempt failed (``reason``); an earlier cut-off, if any, is still the reported one.
``CREATED``
    The month has a cut-off.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import MonthlyCutoff, MonthlyCutoffJob, User
from app.features.cash_statement_service import Period

CUTOFF_JOB, EXPORT_JOB = "CUTOFF", "EXPORT"
JOB_TYPES = (CUTOFF_JOB, EXPORT_JOB)
QUEUED, RUNNING, SUCCEEDED, FAILED = "QUEUED", "RUNNING", "SUCCEEDED", "FAILED"
ACTIVE = (QUEUED, RUNNING)
NOT_READY = "OPERATIONAL_DATA_NOT_READY"
INTERRUPTED = "INTERRUPTED"
ALREADY_RUNNING = "ALREADY_RUNNING"
ERROR = "ERROR"

NOT_ENDED, NOT_CREATED, WAITING_FOR_DATA, CREATED = "NOT_ENDED", "NOT_CREATED", "WAITING_FOR_DATA", "CREATED"
CUTOFF_STATUSES = (NOT_ENDED, NOT_CREATED, QUEUED, RUNNING, WAITING_FOR_DATA, FAILED, CREATED)


def job_timeout() -> timedelta:
    return timedelta(minutes=settings.cutoff_job_timeout_minutes)


def interrupted_reason() -> str:
    return f"The job was interrupted: it had not finished {settings.cutoff_job_timeout_minutes} minutes after it started."


def is_stale(job: MonthlyCutoffJob, now: datetime) -> bool:
    """A ``RUNNING`` job whose worker has died (it has been running too long)."""
    return job.status == RUNNING and job.started_at is not None and job.started_at < now - job_timeout()


def effective(job: MonthlyCutoffJob, now: datetime) -> tuple[str, str | None, str | None]:
    """``(status, reason_code, reason)``, a stale running job counting as interrupted."""
    if is_stale(job, now):
        return FAILED, INTERRUPTED, interrupted_reason()
    return job.status, job.reason_code, job.reason


def is_active(job: MonthlyCutoffJob | None, now: datetime) -> bool:
    return job is not None and effective(job, now)[0] in ACTIVE


def user_names(db: Session, ids: Iterable[int | None]) -> dict[int, str]:
    wanted = {item for item in ids if item}
    if not wanted:
        return {}
    return dict(db.execute(select(User.id, User.name).where(User.id.in_(wanted))).tuples().all())


def job_out(job: MonthlyCutoffJob, names: dict[int, str], now: datetime) -> dict[str, object]:
    status, reason_code, reason = effective(job, now)
    return {
        "id": job.id,
        "type": job.job_type,
        "period": {"year": job.period_start.year, "month": job.period_start.month},
        "status": status,
        "reason_code": reason_code,
        "reason": reason,
        "requested_at": job.requested_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        # None: started by the hourly cron.
        "requested_by": None if job.requested_by is None else {"id": job.requested_by, "name": names.get(job.requested_by)},
        "cutoff_id": job.cutoff_id,
    }


def cutoff_status(
    period: Period, cutoff: MonthlyCutoff | None, job: MonthlyCutoffJob | None, now: datetime
) -> dict[str, object]:
    """The month's cut-off status (see the module docstring). ``cutoff``: its reported cut-off,
    ``job``: its latest ``CUTOFF`` job."""
    if period.ends_at > now:
        return {"status": NOT_ENDED, "reason": "The month has not ended yet.", "can_generate": False}
    if job is not None:
        status, reason_code, reason = effective(job, now)
        if status in ACTIVE:
            return {"status": status, "reason": None, "can_generate": False}
        newer = cutoff is None or job.requested_at > cutoff.started_at
        if status == FAILED and newer:
            return {
                "status": WAITING_FOR_DATA if reason_code == NOT_READY else FAILED,
                "reason": reason,
                "can_generate": True,
            }
    if cutoff is not None:
        return {"status": CREATED, "reason": None, "can_generate": True}
    return {"status": NOT_CREATED, "reason": "The cut-off of this month has not been created yet.", "can_generate": True}
