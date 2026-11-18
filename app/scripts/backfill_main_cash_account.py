"""Populate ``main_cash_accounts`` / ``main_cash_account_logs`` from existing transactions.

This is a manual data migration, independent of the Alembic schema migrations. It reconciles the
main cash account log with ``operational_cashflows`` and ``revenues``:

- every paid ``MAIN_ACCOUNT_TOPUP`` cashflow should be logged as a ``debit`` of its ``amount_idr``;
- every paid (approved) ``MAIN_ACCOUNT_TRANSFER`` revenue should be logged as a ``credit`` of its
  ``amount_idr``.

For each source row the difference between what it should contribute and what the log already
holds for it is posted as one entry with reference ``<table>:<id>:backfill``. Manual adjustments
(``manual:...``) are left untouched. Finally ``current_amount`` is set to the signed sum of the
whole log, which also repairs drift after manual database edits. Running it again posts nothing,
so it is idempotent.

This is the same engine as the daily audit (``app.features.main_cash_account_audit``) run in full
mode: batched, compared inside PostgreSQL and written set-based, so it stays usable on tables with
millions of rows. It never takes the cashflow/revenue write lock, so it can run while the API is live. For
the recurring background check use ``python -m app.scripts.audit_main_cash_account`` instead.

Usage (from the backend directory, with DATABASE_URL set or present in .env)::

    python -m app.scripts.backfill_main_cash_account --dry-run   # show what would be posted
    python -m app.scripts.backfill_main_cash_account             # post it
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.decimal import decimal_string
from app.db.session import SessionLocal
from app.features.main_cash_account_audit import FULL, audit_main_cash_account
from app.features.main_cash_account_service import CREDIT, DEBIT, source_reference


@dataclass(frozen=True)
class PostedEntry:
    reference: str
    direction: str
    amount: Decimal


@dataclass(frozen=True)
class BackfillResult:
    previous_amount: Decimal | None
    new_amount: Decimal
    dry_run: bool
    posted: list[PostedEntry] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.posted) or self.previous_amount != self.new_amount


def backfill_main_cash_account(db: Session, *, dry_run: bool = False) -> BackfillResult:
    """Posts everything missing for existing top-ups/transfers. Commits unless ``dry_run``."""
    result = audit_main_cash_account(mode=FULL, dry_run=dry_run, action="backfill", session=db)
    posted = [
        PostedEntry(
            source_reference(item.source, item.source_id, "backfill"),
            CREDIT if item.delta > 0 else DEBIT,
            abs(item.delta),
        )
        for item in result.posted
    ]
    return BackfillResult(result.previous_amount, result.new_amount or Decimal("0"), dry_run, posted)


def format_amount(value: Decimal | None) -> str:
    return "(no row yet)" if value is None else (decimal_string(value, 0) or "0")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.backfill_main_cash_account",
        description="Post existing main account top-ups/transfers to main_cash_account_logs and recalculate the amount.",
    )
    parser.add_argument("--dry-run", action="store_true", help="calculate and print without writing anything")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        result = backfill_main_cash_account(db, dry_run=args.dry_run)

    credits = sum((entry.amount for entry in result.posted if entry.direction == CREDIT), Decimal("0"))
    debits = sum((entry.amount for entry in result.posted if entry.direction == DEBIT), Decimal("0"))
    rows = [
        ("Log entries to post:", str(len(result.posted))),
        ("  credit total:", format_amount(credits)),
        ("  debit total:", format_amount(debits)),
        ("Previous main cash account amount:", format_amount(result.previous_amount)),
        ("New main cash account amount:", format_amount(result.new_amount)),
    ]
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        print(f"{label:<{width}}{value}")
    if result.dry_run:
        print("Dry run: nothing was written.")
    elif result.changed:
        print("Main cash account updated.")
    else:
        print("Main cash account already up to date; nothing to change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
