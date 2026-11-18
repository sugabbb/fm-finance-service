from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import OperationalCashflow, OperationalCashflowFeedDetail, OperationalIntegrationOutbox, User
from app.features.operational_outbox import deliver_pending, enqueue_feed_projection


def test_feed_projection_is_revisioned_idempotent_and_deliverable(
    db: Session, actor: User, monkeypatch
) -> None:
    cashflow = OperationalCashflow(
        cashflow_type="FEED_AYAM",
        direction="OUTFLOW",
        transaction_date=date(2026, 11, 18),
        amount_idr=Decimal("100000"),
        approval_status="APPROVED",
        payment_status="UNPAID",
        created_by=actor.id,
    )
    db.add(cashflow)
    db.flush()
    line = OperationalCashflowFeedDetail(
        operational_cashflow_id=cashflow.id,
        feed_type_id=9,
        feed_type_code="AYAM-BR-1",
        feed_type_name="BR 1",
        feed_species="AYAM",
        purchase_unit="KG",
        purchase_quantity=Decimal("25"),
        price_per_purchase_unit_idr=Decimal("4000"),
        quantity_kg=Decimal("25"),
        price_per_kg_idr=Decimal("4000"),
        line_amount_idr=Decimal("100000"),
        received_at=datetime(2026, 11, 18, tzinfo=UTC),
        received_by=actor.id,
    )
    db.add(line)
    db.flush()

    enqueue_feed_projection(db, cashflow)
    enqueue_feed_projection(db, cashflow)
    db.commit()
    rows = db.scalars(select(OperationalIntegrationOutbox)).all()
    assert len(rows) == 1
    assert rows[0].source_revision == 1
    assert rows[0].payload["lines"][0]["received_date"] == "2026-11-18"

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "app.features.operational_outbox.operational_client.put_inventory",
        lambda path, payload: sent.append((path, payload)),
    )
    assert deliver_pending(db) == 1
    assert sent[0][0] == f"/api/v1/internal/inventory/feed-receipts/{cashflow.id}"
    db.refresh(rows[0])
    assert rows[0].status == "DELIVERED"
