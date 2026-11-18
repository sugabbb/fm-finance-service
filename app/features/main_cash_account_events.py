"""Background posting of queued main cash account movements (transactional outbox consumer).

Cashflow and revenue transactions insert their main account movement into
``main_cash_account_events`` in the same commit as the business change, without touching the
account row. This module turns that queue into ``main_cash_account_logs`` entries, moves
``current_amount`` and deletes the processed events, each batch in its own transaction that only
holds the account row lock (never the cashflow/revenue write lock).

It is idempotent and safe to run from several places at once, so three triggers share it:

- after every cashflow/revenue API response, through FastAPI ``BackgroundTasks``
  (:func:`drain_in_background`), so the front end never waits for it;
- ``python -m app.scripts.main_cash_account_worker`` for a dedicated process or cron;
- the daily audit, which drains leftovers before checking (safety net if a process died between
  committing a transaction and running its background task).

The resulting ledger is exactly what synchronous posting produced: one entry per change, with the
same ``<table>:<id>:<action>`` reference, and ``created_at`` set to when the change was committed
rather than when it was posted.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import delete, exists, func, insert, select
from sqlalchemy.orm import Session

from app.db.models import MainCashAccountEvent, MainCashAccountLog
from app.db.session import SessionLocal
from app.features.main_cash_account_service import CREDIT, DEBIT, get_or_create_account, source_reference

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    if session is not None:
        yield session
    else:
        with SessionLocal() as owned:
            yield owned


def has_pending(db: Session) -> bool:
    """Unlocked existence check, so an empty queue costs one index lookup and no lock."""
    return bool(db.scalar(select(exists().where(MainCashAccountEvent.id.is_not(None)))))


def pending_summary(db: Session) -> tuple[int, Decimal]:
    """``(count, signed total)`` of movements committed but not posted yet."""
    count, total = db.execute(
        select(func.count(), func.coalesce(func.sum(MainCashAccountEvent.delta), Decimal("0")))
    ).one()
    return int(count), Decimal(total)


def process_batch(db: Session, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Posts up to ``batch_size`` queued events in one transaction. Returns how many were posted."""
    # Lock the account row *before* inserting log rows (see the invariant in
    # main_cash_account_service): this also serialises concurrent workers and the audit.
    account = get_or_create_account(db)
    events = db.scalars(
        select(MainCashAccountEvent)
        .order_by(MainCashAccountEvent.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    if not events:
        db.commit()
        return 0
    db.execute(
        insert(MainCashAccountLog),
        [
            {
                "main_cash_account_id": account.id,
                "amount": abs(Decimal(event.delta)),
                "direction": CREDIT if event.delta > 0 else DEBIT,
                "reference": source_reference(event.source_table, event.source_id, event.action),
                "created_at": event.created_at,
            }
            for event in events
        ],
    )
    account.current_amount = Decimal(account.current_amount) + sum(
        (Decimal(event.delta) for event in events), Decimal("0")
    )
    db.execute(delete(MainCashAccountEvent).where(MainCashAccountEvent.id.in_([event.id for event in events])))
    db.commit()
    return len(events)


def drain(
    *, session: Session | None = None, batch_size: int = DEFAULT_BATCH_SIZE, max_batches: int | None = None
) -> int:
    """Posts queued events until the queue is empty (or ``max_batches`` is reached)."""
    posted = 0
    batches = 0
    with _session(session) as db:
        if not has_pending(db):
            db.rollback()
            return 0
        while max_batches is None or batches < max_batches:
            count = process_batch(db, batch_size)
            if not count:
                break
            posted += count
            batches += 1
    return posted


def drain_in_background() -> None:
    """``BackgroundTasks`` entry point. Never raises: on failure the events simply stay queued and
    are posted by the next trigger (another request, the worker command, or the daily audit)."""
    try:
        posted = drain()
        if posted:
            logger.info("posted %s main cash account events", posted)
    except Exception:
        logger.exception("posting main cash account events failed; they stay queued for the next run")
