"""Monthly cut-off: closes the month that just ended. Run it from cron (the service has no
scheduler), at midnight at the end of every month:

    # 00:00 on the 1st of every month, server time (= APP_TIMEZONE, see sync_cash_statements)
    0 0 1 * *  cd /srv/api && .venv/bin/python -m app.scripts.monthly_cutoff

Like the nightly sync, the month is read from the start time minus 30 minutes, so the run at
00:00 on 1 August closes **July**. It first syncs that month's cash statement (recorded in
``cash_statement_sync_runs``), then saves the month's figures - actual cash, profit and loss,
outstanding loans, receivables and the statement grouped by category (see
``app.features.monthly_cutoff_service``) - as a new snapshot in ``monthly_cutoffs`` and its detail
tables, marked ``is_latest``. It can share the minute with the nightly sync: both take the same
lock and the sync is idempotent.

Re-run a closed month after late entries with ``--month``: it saves a new snapshot, which becomes
the month's ``is_latest``; the earlier ones are kept. A month that has not ended cannot be closed.

Options::

    --month YYYY-MM   close this month instead of the default (must have ended)
    --dry-run         compute and log the figures without saving them (the month's cash
                      statement sync still runs, as it would on any night)

Exit codes: ``0`` success, ``1`` the run failed (recorded as ``FAILED``), ``2`` invalid arguments
or a month that has not ended.
"""

import argparse
import logging
import sys
from collections.abc import Sequence

from app.core.config import settings
from app.features.cash_statement_service import Period
from app.features.monthly_cutoff_service import REVENUE_SECTION, CutoffOutcome, MonthNotClosedError, run_cutoff
from app.scripts.sync_cash_statements import month

logger = logging.getLogger("app.scripts.monthly_cutoff")

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


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
    for warning in figures.warnings:
        logger.warning("%s: %s", prefix, warning)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.monthly_cutoff",
        description="Close a month: aggregate cash_statements into the monthly cut-off tables.",
    )
    parser.add_argument(
        "--month", type=month, help="YYYY-MM, must have ended (default: the month of the start time minus 30 minutes)"
    )
    parser.add_argument("--dry-run", action="store_true", help="compute and log the figures, save nothing")
    args = parser.parse_args(argv)
    period: Period | None = args.month

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        outcome = run_cutoff(period=period, dry_run=args.dry_run)
    except MonthNotClosedError as error:
        logger.error("%s", error)
        return EXIT_USAGE
    except Exception:
        logger.exception("monthly cut-off failed")
        return EXIT_FAILED
    log_outcome(outcome)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
