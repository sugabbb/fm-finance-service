"""Where the Excel export of a cut-off stands, as shown by the API (list and detail) and used to
decide what the automatic retries do (see ``monthly_cutoff_export``).

Statuses:

``NOT_STARTED``
    No generation has been attempted (e.g. a cut-off closed before exports existed, or the
    process stopped between the close and the export).
``QUEUED``
    A user asked for it (``POST /monthly-cutoffs/jobs``); waiting for the worker.
``GENERATING``
    A generation is running.
``SUCCEEDED``
    The workbook is ready to download.
``FAILED``
    The last attempt failed (``reason`` says why), or it was interrupted: it was still
    ``GENERATING`` after ``EXPORT_GENERATION_TIMEOUT_MINUTES`` (the process died).

The hourly ``monthly_cutoff --pending`` run generates ``NOT_STARTED`` exports and retries ``FAILED``
ones until ``EXPORT_MAX_AUTOMATIC_ATTEMPTS`` failures in a row (``automatic_retry``). A user can
queue an export from the frontend at any time except while one is queued or being generated
(``can_generate``), also after the automatic retries gave up. A failed regeneration keeps the file
of the last successful one (``file``).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import MonthlyCutoff, MonthlyCutoffExport, MonthlyCutoffJob
from app.features import monthly_cutoff_job_state as jobs

NOT_STARTED, QUEUED, GENERATING, SUCCEEDED, FAILED = "NOT_STARTED", "QUEUED", "GENERATING", "SUCCEEDED", "FAILED"
EXPORT_STATUSES = (NOT_STARTED, QUEUED, GENERATING, SUCCEEDED, FAILED)
NOT_STARTED_REASON = "The Excel file has not been generated yet."


def generation_timeout() -> timedelta:
    return timedelta(minutes=settings.export_generation_timeout_minutes)


def interrupted_reason() -> str:
    return (
        f"The generation was interrupted: it had not finished {settings.export_generation_timeout_minutes} "
        "minutes after it started."
    )


def is_stale(row: MonthlyCutoffExport, now: datetime) -> bool:
    """A ``GENERATING`` row whose process has died (it has been running too long)."""
    return row.status == GENERATING and row.attempt_started_at < now - generation_timeout()


@dataclass(frozen=True)
class ExportState:
    status: str
    reason: str | None
    failed_attempts: int  # in a row, an interrupted attempt included
    automatic_retry: bool  # the hourly run will (re)try it
    can_generate: bool  # a user may generate it now

    @property
    def needs_automatic_attempt(self) -> bool:
        return self.status in (NOT_STARTED, FAILED) and self.automatic_retry


def export_state(
    row: MonthlyCutoffExport | None, now: datetime, job: MonthlyCutoffJob | None = None
) -> ExportState:
    """``job``: the month's latest ``EXPORT`` job, if any (an active one takes precedence)."""
    limit = settings.export_max_automatic_attempts
    failed_so_far = row.failed_attempts if row else 0
    if job is not None and jobs.is_active(job, now):
        status = QUEUED if job.status == jobs.QUEUED else GENERATING
        return ExportState(status, None, failed_so_far, False, False)
    if row is None:
        return ExportState(NOT_STARTED, NOT_STARTED_REASON, 0, True, True)
    if row.status == GENERATING and not is_stale(row, now):
        return ExportState(GENERATING, None, row.failed_attempts, False, False)
    if row.status == SUCCEEDED:
        return ExportState(SUCCEEDED, None, 0, False, True)
    failed = row.failed_attempts + (1 if row.status == GENERATING else 0)  # + the interrupted attempt
    reason = interrupted_reason() if row.status == GENERATING else row.last_error
    return ExportState(FAILED, reason, failed, failed < limit, True)


def download_path(cutoff: MonthlyCutoff) -> str:
    return f"/api/v1/monthly-cutoffs/export?year={cutoff.period_start.year}&month={cutoff.period_start.month}"


def requester_names(db: Session, rows: Iterable[MonthlyCutoffExport | None]) -> dict[int, str]:
    return jobs.user_names(db, (row.attempt_requested_by for row in rows if row is not None))


def export_out(
    cutoff: MonthlyCutoff,
    row: MonthlyCutoffExport | None,
    now: datetime,
    names: dict[int, str],
    job: MonthlyCutoffJob | None = None,
) -> dict[str, object]:
    """The ``export`` object of a cut-off in the API (list and detail). ``job``: the month's latest
    ``EXPORT`` job."""
    state = export_state(row, now, job)
    requested_by = row.attempt_requested_by if row else None
    return {
        "status": state.status,
        "reason": state.reason,
        "failed_attempts": state.failed_attempts,
        "max_automatic_attempts": settings.export_max_automatic_attempts,
        "automatic_retry": state.automatic_retry,
        "can_generate": state.can_generate,
        "last_attempt": None
        if row is None
        else {
            "started_at": row.attempt_started_at.isoformat(),
            "finished_at": row.attempt_finished_at.isoformat() if row.attempt_finished_at else None,
            "automatic": requested_by is None,
            "requested_by": None if requested_by is None else {"id": requested_by, "name": names.get(requested_by)},
        },
        # The last successful generation; kept when a regeneration fails.
        "file": None
        if row is None or row.storage_key is None
        else {
            "file_name": row.file_name,
            "size_bytes": row.size_bytes,
            "generated_at": row.generated_at.isoformat() if row.generated_at else None,
            "warnings": list(row.warnings or []),
            "download_path": download_path(cutoff),
        },
    }
