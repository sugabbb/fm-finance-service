"""Monthly cut-off: closes the month that just ended. Run it from cron (the service has no
scheduler).

A month can only be closed once operational-service has finalized its month-end data (egg stock,
fish weight estimate; it answers ``404`` until then), which is normally some time after midnight.
So the close is retried until it happens: run it every hour with ``--pending``, which closes the
last ended month if it has not been closed yet and does nothing otherwise::

    # every hour at :05, server time (= APP_TIMEZONE, see sync_cash_statements)
    5 * * * *  cd /srv/api && .venv/bin/python -m app.scripts.monthly_cutoff --pending

While operational's data is not available the command exits with ``3``; the month stays open
(``GET /monthly-cutoffs`` answers 404, the list shows ``WAITING_FOR_DATA``) until a later run
closes it.

Every close runs as a background job (``app.features.monthly_cutoff_jobs``, automatic), like the
ones users queue from the frontend: at most one close of a month runs at a time, and a close
queued from the frontend for the month is taken over. Each ``--pending`` run also runs the jobs
still queued (normally the worker, ``app.scripts.monthly_cutoff_worker``, has done it already).

Without ``--pending`` or ``--month``, like the nightly sync, the month is read from the start time
minus 30 minutes, so a run at 00:00 on 1 August closes **July**. A run first syncs that month's
cash statement (recorded in ``cash_statement_sync_runs``), then saves the month's figures - actual
cash, profit and loss, outstanding loans, receivables, the statement grouped by category and the
potential revenue of the month-end stock (see ``app.features.monthly_cutoff_service``) - as a new
snapshot in ``monthly_cutoffs`` and its detail tables, marked ``is_latest``. It can share the
minute with the nightly sync: both take the same lock and the sync is idempotent.

Right after a close (not a dry run), the month's Excel workbook for the finance team is generated
and stored (``app.features.monthly_cutoff_export``). If that fails the close stays saved and the
command exits with ``1``. Every ``--pending`` run first retries the exports due, of any month:
those not started yet and those that failed fewer than ``EXPORT_MAX_AUTOMATIC_ATTEMPTS`` times in a
row. Past that, a user generates it from the frontend (``POST /monthly-cutoffs/export``) or
``python -m app.scripts.export_monthly_cutoff --month YYYY-MM`` does.

Re-run a closed month after late entries with ``--month``: it saves a new snapshot, which becomes
the month's ``is_latest``; the earlier ones are kept. A month that has not ended cannot be closed.

Options::

    --month YYYY-MM   close this month instead of the default (must have ended)
    --pending         only if the month has not been closed yet; without --month, the month is
                      the last one that has ended (the month before the current one). Also
                      retries the Excel exports due (of any month) first
    --dry-run         compute and log the figures without saving them (the month's cash
                      statement sync still runs, as it would on any night)

Exit codes: ``0`` success (or, with ``--pending``, the month was already closed or is being closed
by a job), ``1`` the run failed (or, with ``--month``, a job is closing the month already) (recorded as ``FAILED`` once it started; an unreachable operational-service or invalid
operational data fails it before it starts) or an Excel export attempt failed (the close, if
any, is saved), ``2`` invalid arguments or a month that has not ended, ``3`` waiting:
operational-service has not published the month-end data yet.
"""

import argparse
import logging
import sys
from collections.abc import Sequence

from app.core.config import settings
from app.db.session import SessionLocal
from app.features.cash_statement_service import Period
from app.features.common import now_utc
from app.features.month_end_assets import EGG_STOCK, FISH_STOCK, MonthEndDataNotReadyError
from app.features.monthly_cutoff_export import retry_exports
from app.features.monthly_cutoff_job_state import CUTOFF_JOB
from app.features.monthly_cutoff_jobs import execute, run_queued, start_automatic
from app.features.monthly_cutoff_service import (
    REVENUE_SECTION,
    CutoffOutcome,
    MonthNotClosedError,
    cutoff_period,
    is_closed,
    last_ended_period,
    run_cutoff,
)
from app.scripts.sync_cash_statements import month

logger = logging.getLogger("app.scripts.monthly_cutoff")

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_WAITING = 0, 1, 2, 3


def log_outcome(outcome: CutoffOutcome) -> None:
    figures = outcome.figures
    cash, pl = figures.cash, figures.profit_loss
    owed = figures.loans_outstanding
    saved = "dry run, not saved" if outcome.dry_run else f"saved as cut-off {outcome.cutoff_id}"
    prefix = f"monthly cut-off {figures.period.label}"
    logger.info("%s: %s (cash statement sync run %s)", prefix, saved, outcome.sync_run_id)
    logger.info(
        "%s: cash opening %s, in %s, out %s, closing %s",
        prefix,
        cash.opening + cash.opening_balances_in_period,
        cash.cash_in,
        cash.cash_out,
        cash.closing,
    )
    logger.info("%s: revenue %s, net profit %s", prefix, pl.total(REVENUE_SECTION), pl.net_profit)
    logger.info(
        "%s: %s loans owed, principal outstanding %s",
        prefix,
        len(owed),
        sum((item.principal_outstanding for item in owed), 0),
    )
    logger.info(
        "%s: receivables %s from %s customers",
        prefix,
        sum((item.outstanding for item in figures.receivables), 0),
        len(figures.receivables),
    )
    potential = figures.potential_revenue
    logger.info(
        "%s: potential revenue %s (eggs %s, fish %s)",
        prefix,
        potential.total(),
        potential.total(EGG_STOCK),
        potential.total(FISH_STOCK),
    )
    for warning in figures.warnings:
        logger.warning("%s: %s", prefix, warning)


def log_waiting(waiting: MonthEndDataNotReadyError) -> None:
    ended = (now_utc() - waiting.period.ends_at).days
    logger.warning(
        "monthly cut-off %s: not closed, waiting for operational-service month-end data (%s day(s) after "
        "the month's end): %s",
        waiting.period.label,
        ended,
        "; ".join(f"{item.path} -> 404 {item.code or ''} {item.detail or ''}".rstrip() for item in waiting.missing),
    )


def retry(dry_run: bool) -> int:
    """The automatic export attempts due (not started, or failed fewer times than the maximum)."""
    if dry_run:
        return EXIT_OK
    try:
        results = retry_exports()
    except Exception:
        logger.exception("monthly cut-off exports: could not retry the exports")
        return EXIT_FAILED
    for result in results:
        if result.error is None:
            logger.info("monthly cut-off %s: Excel export generated (automatic retry)", result.period.label)
        else:
            logger.error("monthly cut-off %s: automatic Excel export attempt failed: %s", result.period.label, result.error)
    return EXIT_FAILED if any(result.error for result in results) else EXIT_OK


def run_queued_jobs(dry_run: bool) -> None:
    """The jobs queued from the frontend that no worker has taken (the worker normally does)."""
    if dry_run:
        return
    try:
        results = run_queued()
    except Exception:
        logger.exception("monthly cut-off: could not run the queued jobs")
        return
    for result in results:
        logger.info(
            "monthly cut-off %s: queued %s job %s %s", result.period.label, result.job_type, result.job_id,
            result.status.lower(),
        )


def dry_close(period: Period) -> int:
    try:
        outcome = run_cutoff(period=period, dry_run=True)
    except MonthEndDataNotReadyError as waiting:
        log_waiting(waiting)
        return EXIT_WAITING
    except Exception:
        logger.exception("monthly cut-off failed")
        return EXIT_FAILED
    log_outcome(outcome)
    return EXIT_OK


def close(period: Period | None, dry_run: bool, *, pending: bool) -> int:
    """Closes the month as a background job (``monthly_cutoff_jobs``, automatic), so it never runs
    at the same time as one queued from the frontend (it takes that one over if it is queued)."""
    try:
        period = cutoff_period(now_utc(), period)
    except MonthNotClosedError as error:
        logger.error("%s", error)
        return EXIT_USAGE
    if dry_run:
        return dry_close(period)
    try:
        with SessionLocal() as db:
            job_id = start_automatic(db, CUTOFF_JOB, period)
    except Exception:
        logger.exception("monthly cut-off failed")
        return EXIT_FAILED
    if job_id is None:
        logger.info("monthly cut-off %s: a cut-off of the month is already running (background job)", period.label)
        return EXIT_OK if pending else EXIT_FAILED
    result = execute(job_id)
    if result.not_ready is not None:
        log_waiting(result.not_ready)
        return EXIT_WAITING
    if result.cutoff is None:
        logger.error("monthly cut-off failed (%s, job %s): %s", period.label, job_id, result.reason)
        return EXIT_FAILED
    log_outcome(result.cutoff)
    if result.export is None:
        logger.error(
            "monthly cut-off %s: the Excel export failed (the close itself is saved): %s; the --pending run "
            "retries it (up to %s failed attempts in a row)",
            period.label,
            result.export_error,
            settings.export_max_automatic_attempts,
        )
        return EXIT_FAILED
    logger.info(
        "monthly cut-off %s: Excel export %s saved (%s bytes, storage key %s)",
        period.label,
        result.export.file_name,
        result.export.size_bytes,
        result.export.storage_key,
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.monthly_cutoff",
        description="Close a month: aggregate cash_statements into the monthly cut-off tables.",
    )
    parser.add_argument(
        "--month", type=month, help="YYYY-MM, must have ended (default: the month of the start time minus 30 minutes)"
    )
    parser.add_argument(
        "--pending",
        action="store_true",
        help="only if the month is not closed yet (default month: the last one that has ended), and retry the "
        "Excel exports due; for an hourly cron",
    )
    parser.add_argument("--dry-run", action="store_true", help="compute and log the figures, save nothing")
    args = parser.parse_args(argv)
    period: Period | None = args.month

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not args.pending:
        return close(period, args.dry_run, pending=False)

    # Retries first: a month closed below makes its own first export attempt, and is retried by the
    # next run, not straight away.
    retried = retry(args.dry_run)
    period = period or last_ended_period(now_utc())
    try:
        closed = is_closed(period)
    except Exception:
        logger.exception("monthly cut-off %s: could not check whether the month is closed", period.label)
        return EXIT_FAILED
    if closed:
        logger.info("monthly cut-off %s: already closed; nothing to close", period.label)
        closing = retried
    else:
        closing = close(period, args.dry_run, pending=True)
    run_queued_jobs(args.dry_run)
    return EXIT_FAILED if EXIT_FAILED in (closing, retried) else closing


if __name__ == "__main__":
    sys.exit(main())
