"""Populate ``cash_statements`` (the bank-statement style record) from the existing transactions.

This is a manual data migration, independent of the Alembic schema migrations, for the one-off
catch-up after ``alembic upgrade head`` or to repair drift from a manual database edit. The
day-to-day population is ``app.scripts.sync_cash_statements`` (run nightly from cron, one month at
a time, recorded in ``cash_statement_sync_runs``); this command instead covers the whole history and
prints its report rather than recording it. It derives every
real cash movement from ``operational_cashflows`` (+ payments), ``revenues`` (+ payments), the
manual adjustments of ``main_cash_account_logs`` and the loan repayments, using the rules documented in
``app.features.cash_statement_service``, and brings the table in line with them:

- movements that are missing are inserted;
- stored rows whose source changed (amount, date, description, status, ...) are updated in place;
- stored rows whose source no longer qualifies (voided payment, unpaid again, deleted) are removed;
- rows that already match are left untouched, so re-running it changes nothing.

Everything is compared and written inside PostgreSQL (set-based), so it stays usable on tables
with millions of rows. It takes the same transactional advisory lock that every cashflow/revenue
write uses, so it can run while the API is live: concurrent writes simply wait until it commits.

Usage (from the backend directory, with DATABASE_URL set or present in .env)::

    python -m app.scripts.backfill_cash_statements --dry-run   # print what would change
    python -m app.scripts.backfill_cash_statements             # write it
"""

import argparse
import sys
from collections.abc import Sequence
from decimal import Decimal

from app.core.decimal import decimal_string
from app.db.session import SessionLocal
from app.features.cash_statement_service import RebuildResult, rebuild_cash_statements


def format_amount(value: Decimal) -> str:
    return decimal_string(value, 0) or "0"


def print_result(result: RebuildResult) -> None:
    rows = [
        ("Statement rows to insert:", str(result.plan.inserted)),
        ("Statement rows to update:", str(result.plan.updated)),
        ("Statement rows to delete:", str(result.plan.deleted)),
    ]
    for total in result.totals:
        rows.append((f"{total.source_table}:", f"{total.rows} rows"))
        rows.append(("  credit total:", format_amount(total.credit)))
        rows.append(("  debit total:", format_amount(total.debit)))
    credit = sum((total.credit for total in result.totals), Decimal("0"))
    debit = sum((total.debit for total in result.totals), Decimal("0"))
    rows.append(("Net (credit - debit):", format_amount(credit - debit)))
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        print(f"{label:<{width}}{value}")
    if result.dry_run:
        print("Dry run: nothing was written.")
    elif result.plan.changed:
        print("Cash statements updated.")
    else:
        print("Cash statements already up to date; nothing to change.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.backfill_cash_statements",
        description="Derive cash_statements from the existing cashflows, revenues and main account adjustments.",
    )
    parser.add_argument("--dry-run", action="store_true", help="report the changes and totals without writing")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        print_result(rebuild_cash_statements(db, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
