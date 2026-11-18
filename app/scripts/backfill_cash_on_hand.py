"""Populate ``operational_cashflow_balances`` from ``operational_cashflows``.

This is a manual data migration, independent of the Alembic schema migrations. For every balance
type it recalculates the amount from every existing operational cashflow and overwrites the
stored balance, so it is idempotent and safe to run at any time (including repeatedly, or later
to repair drift):

``cash_on_hand``
    - ``MAIN_ACCOUNT_TOPUP`` with payment_status ``PAID`` adds ``amount_idr``.
    - Any other cashflow type subtracts what was actually paid: ``amount_idr`` when ``PAID``, the
      sum of its non-voided payments when ``PARTIALLY_PAID``.

``waiting_approval``
    - Every cashflow whose approval_status is ``SUBMITTED`` adds ``amount_idr``.

``approved_unpaid``
    - Every cashflow whose approval_status is ``APPROVED`` adds what is still unpaid on it:
      ``amount_idr`` minus the sum of its non-voided payments.

It takes the same transactional advisory lock that every operational cashflow write uses, so it
can run while the API is live: concurrent cashflow writes simply wait until it commits.

Usage (from the backend directory, with DATABASE_URL set or present in .env)::

    python -m app.scripts.backfill_cash_on_hand --dry-run                 # show what would be written
    python -m app.scripts.backfill_cash_on_hand                           # write every balance
    python -m app.scripts.backfill_cash_on_hand --type approved_unpaid    # write a single balance
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.decimal import decimal_string
from app.db.session import SessionLocal
from app.features.cash_balance_service import (
    APPROVED_UNPAID,
    BALANCE_CALCULATORS,
    BALANCE_TYPES,
    CASH_ON_HAND,
    WAITING_APPROVAL,
    get_balance,
    get_or_create_balance,
)
from app.features.locks import lock_balances

COUNTED_LABELS = {
    CASH_ON_HAND: "Transactions affecting cash on hand:",
    WAITING_APPROVAL: "Transactions waiting for approval:",
    APPROVED_UNPAID: "Approved transactions not fully paid:",
}


@dataclass(frozen=True)
class BackfillResult:
    previous_amount: Decimal | None
    new_amount: Decimal
    counted_transactions: int
    dry_run: bool
    balance_type: str = CASH_ON_HAND

    @property
    def changed(self) -> bool:
        return self.previous_amount != self.new_amount


def backfill_balance(db: Session, balance_type: str, *, dry_run: bool = False) -> BackfillResult:
    """Recalculates one balance type and stores it. Commits unless ``dry_run`` (then rolls back)."""
    calculate = BALANCE_CALCULATORS[balance_type]
    try:
        lock_balances(db)
        existing = get_balance(db, balance_type, lock=True)
        previous = Decimal(existing.amount) if existing else None
        amount, counted = calculate(db)
        if not dry_run:
            balance = get_or_create_balance(db, balance_type)
            balance.amount = amount
        result = BackfillResult(previous, amount, counted, dry_run, balance_type)
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def backfill_cash_on_hand(db: Session, *, dry_run: bool = False) -> BackfillResult:
    return backfill_balance(db, CASH_ON_HAND, dry_run=dry_run)


def backfill_waiting_approval(db: Session, *, dry_run: bool = False) -> BackfillResult:
    return backfill_balance(db, WAITING_APPROVAL, dry_run=dry_run)


def backfill_approved_unpaid(db: Session, *, dry_run: bool = False) -> BackfillResult:
    return backfill_balance(db, APPROVED_UNPAID, dry_run=dry_run)


def format_amount(value: Decimal | None) -> str:
    return "(no row yet)" if value is None else (decimal_string(value, 0) or "0")


def print_result(result: BackfillResult) -> None:
    name = result.balance_type
    rows = [
        (COUNTED_LABELS[name], str(result.counted_transactions)),
        (f"Previous {name} amount:", format_amount(result.previous_amount)),
        (f"Recalculated {name} amount:", format_amount(result.new_amount)),
    ]
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        print(f"{label:<{width}}{value}")
    if result.dry_run:
        print("Dry run: nothing was written.")
    elif result.changed:
        print(f"{name} balance updated.")
    else:
        print(f"{name} balance already up to date; nothing to change.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.backfill_cash_on_hand",
        description="Recalculate the operational cashflow balances from operational_cashflows and save them.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="calculate and print the balances without writing anything",
    )
    parser.add_argument(
        "--type",
        dest="balance_type",
        choices=BALANCE_TYPES,
        help="only backfill this balance type (default: every type)",
    )
    args = parser.parse_args(argv)
    balance_types = [args.balance_type] if args.balance_type else list(BALANCE_TYPES)

    with SessionLocal() as db:
        for index, balance_type in enumerate(balance_types):
            if index:
                print()
            print_result(backfill_balance(db, balance_type, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
