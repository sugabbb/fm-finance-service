"""Daily audit of the main cash account. Run it from cron (the service has no scheduler).

It cross-checks ``main_cash_account_logs`` against ``operational_cashflows`` and ``revenues`` in
both directions, posts a compensating entry for every difference it finds, and recalculates
``current_amount``. Each run is recorded in ``main_cash_account_audit_runs``.

    # every day at 01:15, only what changed since the previous successful run
    15 1 * * *  cd /srv/api && .venv/bin/python -m app.scripts.audit_main_cash_account --mode incremental

    # every Sunday at 02:15, re-check everything
    15 2 * * 0  cd /srv/api && .venv/bin/python -m app.scripts.audit_main_cash_account --mode full

Options::

    --mode {full,incremental}  incremental audits only sources touched since the last successful
                               run (falls back to full when there is none); default full
    --batch-size N             source rows per transaction (default 5000)
    --overlap-hours H          how far before the previous run an incremental run starts (default 24)
    --dry-run                  report what would be posted, write nothing
    --fail-on-discrepancy      exit non-zero when differences were found, for alerting

Exit codes: ``0`` success, ``2`` discrepancies found with ``--fail-on-discrepancy``, ``3`` another
run is still in progress, ``1`` the audit failed.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from app.core.config import settings
from app.core.decimal import decimal_string
from app.features.main_cash_account_audit import (
    DEFAULT_BATCH_SIZE,
    FULL,
    INCREMENTAL,
    AuditResult,
    audit_main_cash_account,
)

logger = logging.getLogger("app.scripts.audit_main_cash_account")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DISCREPANCIES = 2
EXIT_ALREADY_RUNNING = 3


def format_amount(value: Decimal | None) -> str:
    return "(no row yet)" if value is None else (decimal_string(value, 0) or "0")


def print_result(result: AuditResult) -> None:
    rows = [
        ("Mode:", result.mode + (" (dry run)" if result.dry_run else "")),
        ("Status:", result.status),
        ("Window from:", result.window_from.isoformat() if result.window_from else "(everything)"),
        ("Source rows checked:", str(result.sources_checked)),
        ("Batches:", str(result.batches)),
        ("Discrepancies found:", str(result.discrepancies)),
        ("Entries posted:", str(result.entries_posted)),
        ("  credit total:", format_amount(result.credit_posted)),
        ("  debit total:", format_amount(result.debit_posted)),
        ("Previous amount:", format_amount(result.previous_amount)),
        ("New amount:", format_amount(result.new_amount)),
        ("Duration:", f"{result.duration_seconds:.1f}s"),
    ]
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        print(f"{label:<{width}}{value}")
    for item in result.posted[:20]:
        print(f"  fixed {item.source}:{item.source_id} expected={item.expected} logged={item.logged}")
    if result.entries_posted > 20:
        print(f"  ... and {result.entries_posted - 20} more")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.audit_main_cash_account",
        description="Cross-check the main cash account log against its sources and recalculate the amount.",
    )
    parser.add_argument("--mode", choices=(FULL, INCREMENTAL), default=FULL)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--overlap-hours", type=float, default=24.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fail-on-discrepancy", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        result = audit_main_cash_account(
            mode=args.mode,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            overlap=timedelta(hours=args.overlap_hours),
        )
    except Exception:
        logger.exception("main cash account audit failed")
        return EXIT_FAILED

    print_result(result)
    if result.status == "SKIPPED":
        return EXIT_ALREADY_RUNNING
    if result.discrepancies and args.fail_on_discrepancy:
        return EXIT_DISCREPANCIES
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
