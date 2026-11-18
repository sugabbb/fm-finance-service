import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.decimal import decimal_string
from app.db.models import (
    OperationalCashflow,
    OperationalCashflowFeedDetail,
    OperationalIntegrationOutbox,
    Revenue,
    RevenueEggDetail,
)
from app.integrations.operational import operational_client


def _hash(payload: dict[str, Any]) -> str:
    semantic = {key: value for key, value in payload.items() if key != "source_revision"}
    return hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _enqueue(db: Session, aggregate_type: str, aggregate: OperationalCashflow | Revenue, path: str, payload: dict[str, Any]) -> None:
    digest = _hash(payload)
    latest = db.scalar(
        select(OperationalIntegrationOutbox)
        .where(
            OperationalIntegrationOutbox.aggregate_type == aggregate_type,
            OperationalIntegrationOutbox.aggregate_id == aggregate.id,
        )
        .order_by(OperationalIntegrationOutbox.source_revision.desc())
        .limit(1)
    )
    if latest and latest.payload_hash == digest:
        return
    aggregate.operational_revision = int(aggregate.operational_revision or 0) + 1
    payload["source_revision"] = aggregate.operational_revision
    if latest and latest.status in ("PENDING", "FAILED"):
        latest.status = "SUPERSEDED"
    db.add(
        OperationalIntegrationOutbox(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate.id,
            source_revision=aggregate.operational_revision,
            target_path=path,
            payload=payload,
            payload_hash=digest,
            status="PENDING",
        )
    )


def enqueue_feed_projection(db: Session, row: OperationalCashflow) -> None:
    if row.cashflow_type not in ("FEED_AYAM", "FEED_LELE"):
        return
    db.flush()
    lines = db.scalars(
        select(OperationalCashflowFeedDetail)
        .where(OperationalCashflowFeedDetail.operational_cashflow_id == row.id)
        .order_by(OperationalCashflowFeedDetail.id)
    ).all()
    active = row.approval_status == "APPROVED"
    projected = [line for line in lines if active and line.received_at]
    payload: dict[str, Any] = {
        "source_service": "finance-service",
        "source_revision": 0,
        "status": "ACTIVE" if projected else "VOIDED",
        "received_date": None,
        "lines": [
            {
                "source_line_id": str(line.id),
                "feed_type_code": line.feed_type_code,
                "quantity_kg": decimal_string(line.quantity_kg),
                "received_date": line.received_at.date().isoformat(),
            }
            for line in projected
        ],
    }
    _enqueue(db, "FEED_CASHFLOW", row, f"/api/v1/internal/inventory/feed-receipts/{row.id}", payload)


def enqueue_egg_projection(db: Session, row: Revenue) -> None:
    if row.revenue_type != "EGG_SALE":
        return
    db.flush()
    lines = db.scalars(
        select(RevenueEggDetail).where(RevenueEggDetail.revenue_id == row.id).order_by(RevenueEggDetail.id)
    ).all()
    active = row.approval_status not in ("DRAFT", "REJECTED")
    payload: dict[str, Any] = {
        "source_service": "finance-service",
        "source_revision": 0,
        "status": "ACTIVE" if active and lines else "VOIDED",
        "transaction_date": row.transaction_date.isoformat(),
        "lines": [
            {
                "source_line_id": str(line.id),
                "egg_category": line.egg_category,
                "weight_kg": decimal_string(line.weight_kg),
                "quantity_pieces": line.quantity_pieces,
            }
            for line in lines
        ] if active else [],
    }
    _enqueue(db, "EGG_REVENUE", row, f"/api/v1/internal/inventory/egg-sales/{row.id}", payload)


def deliver_pending(db: Session, limit: int = 50) -> int:
    now = datetime.now(UTC)
    rows = db.scalars(
        select(OperationalIntegrationOutbox)
        .where(
            OperationalIntegrationOutbox.status.in_(("PENDING", "FAILED")),
            or_(OperationalIntegrationOutbox.next_attempt_at.is_(None), OperationalIntegrationOutbox.next_attempt_at <= now),
        )
        .order_by(OperationalIntegrationOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    delivered = 0
    for row in rows:
        row.status = "PROCESSING"
        row.attempts += 1
        try:
            operational_client.put_inventory(row.target_path, row.payload)
        except Exception as exc:
            row.status = "FAILED"
            row.last_error = str(exc)[:2000]
            row.next_attempt_at = now + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
        else:
            row.status = "DELIVERED"
            row.last_error = None
            row.next_attempt_at = None
            row.processed_at = now
            delivered += 1
    db.commit()
    return delivered


def drain_in_background() -> None:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        deliver_pending(db)
