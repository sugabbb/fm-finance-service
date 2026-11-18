"""Nightly population of ``cash_statements``, one month at a time. Run it from cron (the service
has no scheduler).

``cash_statements`` (the bank-statement view consumed by the monthly cut-off module) is not
kept current by the cashflow/revenue/loan writes themselves; this command brings it up to date once
a day:

    # every day at 00:00, server time - see APP_TIMEZONE below
    0 0 * * *  cd /srv/api && .venv/bin/python -m app.scripts.sync_cash_statements

Each run syncs **one month** only, not the whole history (that is the one-off
``app.scripts.backfill_cash_statements``): by default the month of the job's start time minus 30
minutes, in ``APP_TIMEZONE``. The run at 00:00 on 1 August therefore reads 23:30 on 31 July and
syncs July - the month whose last day just ended - rather than an August with no data yet; every
other night it is simply the current month. It re-derives only that month's transactions - money
moved during the month, changed during the month (plus a one-day overlap), or already on the
month's statement - and all of their rows (see
``app.features.cash_statement_service.period_scope``). Anything else is left alone; a change to an
older transaction that none of those catch (e.g. a customer renamed months later) is picked up by
the backfill.

Every run is recorded in ``cash_statement_sync_runs`` (month, status, transactions checked, rows
inserted/updated/deleted, the month's totals, the error of a failed run). There is no API for it:
maintainers query the table, e.g. to spot a night that did not run (then re-run that month with
``--month``).

Midnight is deliberate: the run takes the same advisory lock as every cashflow/revenue/loan write
(see ``app.features.farm_service.lock_balances``), so it briefly queues them behind it; the
quietest point of the day keeps that unnoticeable. Schedule it in the cron/systemd-timer timezone
that matches ``APP_TIMEZONE`` (``Asia/Jakarta`` by default) - cron has no notion of the application's
timezone, so on a server whose clock is UTC set ``CRON_TZ``/``OnCalendar``'s timezone or shift the
hour. The run is idempotent (it writes only differences), so running it twice or late is harmless.

Options::

    --month YYYY-MM   sync this month instead of the default (e.g. to catch up a missed night)
    --dry-run         report what would change without writing it (still recorded, flagged dry_run)

Exit codes: ``0`` success (including a dry run), ``1`` the run failed, ``2`` invalid arguments.
"""

import argparse
import logging
import sys
from collections.abc import Sequence

from app.core.config import settings
from app.features.cash_statement_service import Period
from app.features.cash_statement_sync import SyncRunOutcome, run_sync

logger = logging.getLogger("app.scripts.sync_cash_statements")


def month(value: str) -> Period:
    try:
        return Period.parse(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def log_outcome(outcome: SyncRunOutcome) -> None:
    result = outcome.result
    plan, scope, totals = result.plan, result.scope, result.totals
    prefix = f"cash statements {result.period.label} (run {outcome.run_id})"
    logger.info("%s: %s transactions checked", prefix, scope.total)
    if not plan.changed:
        logger.info("%s: already up to date; nothing to change", prefix)
    else:
        logger.info(
            "%s %s: %s inserted, %s updated, %s deleted",
            prefix,
            "would change" if result.dry_run else "changed",
            plan.inserted,
            plan.updated,
            plan.deleted,
        )
    logger.info("%s: %s rows, credit %s, debit %s", prefix, totals.rows, totals.credit, totals.debit)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.sync_cash_statements",
        description="Nightly sync of one month of cash_statements from operational_cashflows, revenues, "
        "main_cash_account_logs and loans.",
    )
    parser.add_argument("--month", type=month, help="YYYY-MM (default: the month of the start time minus 30 minutes)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        outcome = run_sync(period=args.month, dry_run=args.dry_run)
    except Exception:
        logger.exception("syncing cash statements failed")
        return 1
    log_outcome(outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
