"""The nightly cash statement sync with its run history (``cash_statement_sync_runs``).

:func:`run_sync` syncs one month (:func:`app.features.cash_statement_service.sync_period`) and
records the run: a ``RUNNING`` row committed before the sync starts, updated to ``SUCCESS`` with
what changed and the month's totals, or to ``FAILED`` with the error. The history is for
maintainers, who read the table directly (there is no API for it).

The one-off backfill (``app.scripts.backfill_cash_statements``) does not go through here and is
not recorded: it is a manual repair whose report is printed to the operator.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import CashStatementSyncRun
from app.db.session import SessionLocal
from app.features.cash_statement_service import (
    CASHFLOW_SOURCE,
    LOAN_SOURCE,
    LOG_SOURCE,
    REVENUE_SOURCE,
    Period,
    PeriodSyncResult,
    nightly_period,
    sync_period,
)
from app.features.common import now_utc

logger = logging.getLogger(__name__)

RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
FAILED = "FAILED"


@dataclass(frozen=True)
class SyncRunOutcome:
    run_id: int
    result: PeriodSyncResult


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    if session is not None:
        yield session
    else:
        with SessionLocal() as owned:
            yield owned


def run_sync(*, period: Period | None = None, dry_run: bool = False, session: Session | None = None) -> SyncRunOutcome:
    """Syncs ``period`` (default: :func:`nightly_period` of the run's start time, i.e. the month of
    the start time minus 30 minutes in ``APP_TIMEZONE``) and records the run. Re-raises the error of
    a failed sync after recording it."""
    started_at = now_utc()
    period = period or nightly_period(started_at)
    with _session(session) as db:
        run = CashStatementSyncRun(
            period_start=period.start,
            period_end=period.end,
            changed_since=period.changed_since,
            status=RUNNING,
            dry_run=dry_run,
            started_at=started_at,
        )
        db.add(run)
        db.commit()
        try:
            result = sync_period(db, period, dry_run=dry_run)
        except Exception as exc:
            try:
                db.rollback()  # sync_period already did; the session must be usable for the update
                run.status, run.finished_at, run.error = FAILED, now_utc(), f"{type(exc).__name__}: {exc}"
                db.commit()
            except Exception:
                # E.g. the database went away: the row stays RUNNING, the sync's error wins.
                logger.exception("could not record cash statement sync run %s as failed", run.id)
            raise
        _record_success(run, result)
        db.commit()
        return SyncRunOutcome(run.id, result)


def _record_success(run: CashStatementSyncRun, result: PeriodSyncResult) -> None:
    scope, plan, totals = result.scope, result.plan, result.totals
    run.status = SUCCESS
    run.finished_at = now_utc()
    run.cashflows_checked = len(scope.of(CASHFLOW_SOURCE))
    run.revenues_checked = len(scope.of(REVENUE_SOURCE))
    run.adjustments_checked = len(scope.of(LOG_SOURCE))
    run.loans_checked = len(scope.of(LOAN_SOURCE))
    run.transactions_checked = scope.total
    run.rows_inserted, run.rows_updated, run.rows_deleted = plan.inserted, plan.updated, plan.deleted
    run.period_rows, run.period_credit, run.period_debit = totals.rows, totals.credit, totals.debit
