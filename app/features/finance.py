from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.decimal import decimal_string
from app.core.dependencies import ActiveUser, DbSession, Superadmin
from app.core.errors import not_found
from app.core.pagination import PageDep
from app.db.models import (
    AuditEvent,
    OperationalCashflow,
    OperationalCashflowBankAdminDetail,
    OperationalCashflowFeedDetail,
    OperationalCashflowHealthDetail,
    OperationalCashflowLivestockPurchaseDetail,
    OperationalCashflowOtherDetail,
    OperationalCashflowPayment,
    OperationalCashflowSalaryDetail,
    OperationalCashflowTopupDetail,
    OperationalCashflowUtilityDetail,
    Revenue,
    RevenueOtherDetail,
    RevenuePayment,
    RevenueTransferDetail,
)
from app.features.cash_balance_service import cash_on_hand
from app.features.common import actor_name, audit, model_snapshot, now_utc
from app.features.finance_schemas import (
    Decision,
    OperationalCorrection,
    OperationalInput,
    PaymentInput,
    ReceiveFeed,
    RevenueCorrection,
    RevenueInput,
    VoidPayment,
)
from app.features.finance_service import (
    decide_operational,
    decide_revenue,
    pay_operational,
    pay_revenue,
    remaining,
    save_operational,
    save_revenue,
    submit_operational,
    submit_revenue,
    void_operational_payment,
    void_revenue_payment,
)
from app.features.main_cash_account_events import drain_in_background
from app.features.operational_outbox import drain_in_background as drain_operational_outbox
from app.features.operational_outbox import enqueue_feed_projection
from app.integrations.proofs import proof_storage

router = APIRouter(tags=["Finance"])


def payment_out(db: DbSession, row: OperationalCashflowPayment | RevenuePayment) -> dict[str, object]:
    return {
        "id": row.id,
        "payment_mode": row.payment_mode,
        "paid_date": row.paid_date.isoformat(),
        "amount_idr": decimal_string(row.amount_idr, 0),
        "proof_url": row.proof_url,
        "created_by": row.created_by,
        "creator_name": actor_name(db, row.created_by),
        "created_at": row.created_at.isoformat(),
        "voided_at": row.voided_at.isoformat() if row.voided_at else None,
        "voided_by": row.voided_by,
        "voider_name": actor_name(db, row.voided_by),
        "void_reason": row.void_reason,
    }


def audits(db: DbSession, entity_type: str, entity_id: int) -> list[dict[str, object]]:
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.entity_type == entity_type, AuditEvent.entity_id == entity_id)
        .order_by(AuditEvent.happened_at.desc(), AuditEvent.id.desc())
    ).all()
    return [
        {
            "id": row.id,
            "action": row.action,
            "actor_id": row.actor_id,
            "actor_name": actor_name(db, row.actor_id),
            "happened_at": row.happened_at.isoformat(),
            "reason": row.reason,
            "before": row.before,
            "after": row.after,
        }
        for row in rows
    ]


def operational_query():
    return select(OperationalCashflow).options(
        selectinload(OperationalCashflow.feed_details),
        selectinload(OperationalCashflow.payments),
    )


def op_details(db: DbSession, row: OperationalCashflow) -> object:
    if row.cashflow_type in ("FEED_AYAM", "FEED_LELE"):
        return [
            {
                "id": line.id,
                "feed_type_id": line.feed_type_id,
                "feed_name": line.feed_type_name,
                "feed_type_code": line.feed_type_code,
                "feed_species": line.feed_species,
                "purchase_unit": line.purchase_unit,
                "purchase_quantity": decimal_string(line.purchase_quantity),
                "kg_per_sak": decimal_string(line.kg_per_sak),
                "price_per_purchase_unit_idr": decimal_string(line.price_per_purchase_unit_idr),
                "quantity_kg": decimal_string(line.quantity_kg),
                "price_per_kg_idr": decimal_string(line.price_per_kg_idr),
                "line_amount_idr": decimal_string(line.line_amount_idr, 0),
                "received_at": line.received_at.isoformat() if line.received_at else None,
                "received_by": line.received_by,
            }
            for line in row.feed_details
        ]
    model = {
        "SALARY": OperationalCashflowSalaryDetail,
        "UTILITY": OperationalCashflowUtilityDetail,
        "HEALTH": OperationalCashflowHealthDetail,
        "OTHER": OperationalCashflowOtherDetail,
        "LELE_SEED_PURCHASE": OperationalCashflowLivestockPurchaseDetail,
        "PULLET_PURCHASE": OperationalCashflowLivestockPurchaseDetail,
        "BANK_ADMIN_FEE": OperationalCashflowBankAdminDetail,
        "MAIN_ACCOUNT_TOPUP": OperationalCashflowTopupDetail,
    }[row.cashflow_type]
    detail = db.scalar(select(model).where(model.operational_cashflow_id == row.id))
    return model_snapshot(detail) if detail else None


def operational_out(db: DbSession, row: OperationalCashflow) -> dict[str, object]:
    paid = Decimal(row.amount_idr) - remaining(row)
    return {
        "id": row.id,
        "cashflow_type": row.cashflow_type,
        "direction": row.direction,
        "transaction_date": row.transaction_date.isoformat(),
        "amount_idr": decimal_string(row.amount_idr, 0),
        "approval_status": row.approval_status,
        "payment_status": row.payment_status,
        "rejection_reason": row.rejection_reason,
        "proof_url": row.proof_url,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "rejected_at": row.rejected_at.isoformat() if row.rejected_at else None,
        "paid_at": row.paid_at.isoformat() if row.paid_at else None,
        "details": op_details(db, row),
        "paid_amount_idr": decimal_string(paid, 0),
        "remaining_amount_idr": decimal_string(remaining(row), 0),
        "payments": [payment_out(db, payment) for payment in row.payments],
        "audit_logs": audits(db, "operational_cashflows", row.id),
    }


def revenue_query():
    return select(Revenue).options(
        selectinload(Revenue.customer),
        selectinload(Revenue.egg_details),
        selectinload(Revenue.fish_detail),
        selectinload(Revenue.payments),
    )


def revenue_details(db: DbSession, row: Revenue) -> object:
    if row.revenue_type == "EGG_SALE":
        return [
            {
                "id": line.id,
                "egg_category": line.egg_category,
                "weight_kg": decimal_string(line.weight_kg),
                "quantity_pieces": line.quantity_pieces,
                "price_per_kg_idr": decimal_string(line.price_per_kg_idr),
                "line_amount_idr": decimal_string(line.line_amount_idr, 0),
                "notes": line.notes,
            }
            for line in row.egg_details
        ]
    if row.revenue_type == "FISH_SALE":
        return model_snapshot(row.fish_detail) if row.fish_detail else None
    model = RevenueTransferDetail if row.revenue_type == "MAIN_ACCOUNT_TRANSFER" else RevenueOtherDetail
    detail = db.scalar(select(model).where(model.revenue_id == row.id))
    return model_snapshot(detail) if detail else None


def revenue_out(db: DbSession, row: Revenue) -> dict[str, object]:
    paid = Decimal(row.amount_idr) - remaining(row)
    return {
        "id": row.id,
        "revenue_type": row.revenue_type,
        "direction": row.direction,
        "transaction_date": row.transaction_date.isoformat(),
        "amount_idr": decimal_string(row.amount_idr, 0),
        "payment_method": row.payment_method,
        "direct_transfer_date": row.direct_transfer_date.isoformat() if row.direct_transfer_date else None,
        "customer": model_snapshot(row.customer) if row.customer else None,
        "approval_status": row.approval_status,
        "payment_status": row.payment_status,
        "rejection_reason": row.rejection_reason,
        "proof_url": row.proof_url,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "rejected_at": row.rejected_at.isoformat() if row.rejected_at else None,
        "paid_at": row.paid_at.isoformat() if row.paid_at else None,
        "details": revenue_details(db, row),
        "paid_amount_idr": decimal_string(paid, 0),
        "remaining_amount_idr": decimal_string(remaining(row), 0),
        "payments": [payment_out(db, payment) for payment in row.payments],
        "audit_logs": audits(db, "revenues", row.id),
    }


@router.get("/operational-cashflows")
def operational_list(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    cashflow_type: str | None = None,
    approval_status: str | None = None,
    payment_status: str | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    stmt = operational_query()
    for field, value in (
        (OperationalCashflow.cashflow_type, cashflow_type),
        (OperationalCashflow.approval_status, approval_status),
        (OperationalCashflow.payment_status, payment_status),
    ):
        if value:
            stmt = stmt.where(field == value)
    if from_date:
        stmt = stmt.where(OperationalCashflow.transaction_date >= from_date)
    if to_date:
        stmt = stmt.where(OperationalCashflow.transaction_date <= to_date)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(
        stmt.order_by(OperationalCashflow.transaction_date.desc(), OperationalCashflow.id.desc())
        .offset(page.offset)
        .limit(page.per_page)
    ).all()
    return {"data": [operational_out(db, row) for row in rows], "meta": page.meta(total)}


@router.get("/operational-cashflows/cash-on-hand")
def operational_cash_on_hand(_: ActiveUser, db: DbSession) -> dict[str, object]:
    balance = cash_on_hand(db)
    return {
        "data": {
            "amount": decimal_string(balance.amount, 0),
            "updated_at": balance.updated_at.isoformat(),
        }
    }


@router.get("/operational-cashflows/{cashflow_id}")
def operational_get(cashflow_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.scalar(operational_query().where(OperationalCashflow.id == cashflow_id))
    if not row:
        raise not_found("Operational cashflow")
    return {"data": operational_out(db, row)}


@router.post("/operational-cashflows", status_code=status.HTTP_201_CREATED)
def operational_create(
    payload: OperationalInput, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = save_operational(db, payload.model_dump(), actor)
    db.commit()
    # Main cash account posting happens after the response is sent (see main_cash_account_events).
    background_tasks.add_task(drain_in_background)
    return operational_get(row.id, actor, db)


@router.patch("/operational-cashflows/{cashflow_id}")
def operational_update(
    cashflow_id: int, payload: OperationalInput, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = db.get(OperationalCashflow, cashflow_id)
    if not row:
        raise not_found("Operational cashflow")
    row = save_operational(db, payload.model_dump(), actor, row)
    db.commit()
    background_tasks.add_task(drain_in_background)
    return operational_get(row.id, actor, db)


@router.post("/operational-cashflows/{cashflow_id}/correction")
def operational_correct(
    cashflow_id: int,
    payload: OperationalCorrection,
    actor: Superadmin,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    row = db.get(OperationalCashflow, cashflow_id)
    if not row:
        raise not_found("Operational cashflow")
    row = save_operational(
        db, payload.transaction.model_dump(), actor, row, correction=True, reason=payload.correction_reason
    )
    db.commit()
    background_tasks.add_task(drain_in_background)
    return operational_get(row.id, actor, db)


@router.post("/operational-cashflows/{cashflow_id}/submit")
def operational_submit(cashflow_id: int, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(OperationalCashflow, cashflow_id)
    if not row:
        raise not_found("Operational cashflow")
    submit_operational(db, row, actor)
    db.commit()
    return operational_get(row.id, actor, db)


@router.post("/operational-cashflows/{cashflow_id}/decision")
def operational_decision(cashflow_id: int, payload: Decision, actor: Superadmin, db: DbSession) -> dict[str, object]:
    row = db.get(OperationalCashflow, cashflow_id)
    if not row:
        raise not_found("Operational cashflow")
    decide_operational(db, row, actor, payload.decision, payload.reason)
    db.commit()
    return operational_get(row.id, actor, db)


@router.post("/operational-cashflows/{cashflow_id}/payments", status_code=status.HTTP_201_CREATED)
def operational_payment(cashflow_id: int, payload: PaymentInput, actor: Superadmin, db: DbSession) -> dict[str, object]:
    row = db.get(OperationalCashflow, cashflow_id)
    if not row:
        raise not_found("Operational cashflow")
    pay_operational(db, row, actor, payload.paid_date, payload.payment_mode, payload.amount_idr)
    db.commit()
    return operational_get(row.id, actor, db)


@router.patch("/operational-cashflows/{cashflow_id}/payments/{payment_id}/void")
def operational_void(
    cashflow_id: int, payment_id: int, payload: VoidPayment, actor: Superadmin, db: DbSession
) -> dict[str, object]:
    row, payment = db.get(OperationalCashflow, cashflow_id), db.get(OperationalCashflowPayment, payment_id)
    if not row or not payment:
        raise not_found("Operational payment")
    void_operational_payment(db, row, payment, actor, payload.reason)
    db.commit()
    return operational_get(row.id, actor, db)


@router.post("/operational-cashflows/feed-lines/{line_id}/receive")
def receive_feed(line_id: int, payload: ReceiveFeed, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    line = db.get(OperationalCashflowFeedDetail, line_id)
    if not line:
        raise not_found("Feed line")
    parent = db.get(OperationalCashflow, line.operational_cashflow_id)
    if parent.approval_status != "APPROVED":
        from app.core.errors import conflict

        raise conflict("INVALID_STATE", "Feed can be received only after approval.")
    if not line.received_at:
        line.received_at, line.received_by = (
            datetime.combine(payload.received_date, datetime.min.time(), tzinfo=now_utc().tzinfo),
            actor.id,
        )
        from app.features.common import audit, model_snapshot

        audit(db, line, "feed_received", actor)
        enqueue_feed_projection(db, parent)
        db.commit()
        drain_operational_outbox()
    return {"data": model_snapshot(line)}


@router.get("/revenues")
def revenue_list(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    revenue_type: str | None = None,
    payment_method: str | None = None,
    payment_status: str | None = None,
    approval_status: str | None = None,
    customer_id: int | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    stmt = revenue_query()
    for field, value in (
        (Revenue.revenue_type, revenue_type),
        (Revenue.payment_method, payment_method),
        (Revenue.payment_status, payment_status),
        (Revenue.approval_status, approval_status),
        (Revenue.customer_id, customer_id),
    ):
        if value is not None:
            stmt = stmt.where(field == value)
    if from_date:
        stmt = stmt.where(Revenue.transaction_date >= from_date)
    if to_date:
        stmt = stmt.where(Revenue.transaction_date <= to_date)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Revenue.transaction_date.desc(), Revenue.id.desc()).offset(page.offset).limit(page.per_page)
    ).all()
    return {"data": [revenue_out(db, row) for row in rows], "meta": page.meta(total)}


@router.get("/revenues/{revenue_id}")
def revenue_get(revenue_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.scalar(revenue_query().where(Revenue.id == revenue_id))
    if not row:
        raise not_found("Revenue")
    return {"data": revenue_out(db, row)}


@router.post("/revenues", status_code=status.HTTP_201_CREATED)
def revenue_create(
    payload: RevenueInput, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = save_revenue(db, payload.model_dump(), actor)
    db.commit()
    background_tasks.add_task(drain_in_background)
    return revenue_get(row.id, actor, db)


@router.patch("/revenues/{revenue_id}")
def revenue_update(
    revenue_id: int, payload: RevenueInput, actor: ActiveUser, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = db.get(Revenue, revenue_id)
    if not row:
        raise not_found("Revenue")
    row = save_revenue(db, payload.model_dump(), actor, row)
    db.commit()
    background_tasks.add_task(drain_in_background)
    return revenue_get(row.id, actor, db)


@router.post("/revenues/{revenue_id}/correction")
def revenue_correct(
    revenue_id: int, payload: RevenueCorrection, actor: Superadmin, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = db.get(Revenue, revenue_id)
    if not row:
        raise not_found("Revenue")
    row = save_revenue(
        db, payload.transaction.model_dump(), actor, row, correction=True, reason=payload.correction_reason
    )
    db.commit()
    background_tasks.add_task(drain_in_background)
    return revenue_get(row.id, actor, db)


@router.post("/revenues/{revenue_id}/submit")
def revenue_submit(revenue_id: int, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(Revenue, revenue_id)
    if not row:
        raise not_found("Revenue")
    submit_revenue(db, row, actor)
    db.commit()
    return revenue_get(row.id, actor, db)


@router.post("/revenues/{revenue_id}/decision")
def revenue_decision(
    revenue_id: int, payload: Decision, actor: Superadmin, db: DbSession, background_tasks: BackgroundTasks
) -> dict[str, object]:
    row = db.get(Revenue, revenue_id)
    if not row:
        raise not_found("Revenue")
    decide_revenue(db, row, actor, payload.decision, payload.reason, payload.acknowledge_negative_balance)
    db.commit()
    background_tasks.add_task(drain_in_background)
    return revenue_get(row.id, actor, db)


@router.post("/revenues/{revenue_id}/payments", status_code=status.HTTP_201_CREATED)
def revenue_payment(revenue_id: int, payload: PaymentInput, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(Revenue, revenue_id)
    if not row:
        raise not_found("Revenue")
    pay_revenue(db, row, actor, payload.paid_date, payload.payment_mode, payload.amount_idr)
    db.commit()
    return revenue_get(row.id, actor, db)


@router.patch("/revenues/{revenue_id}/payments/{payment_id}/void")
def revenue_void(
    revenue_id: int, payment_id: int, payload: VoidPayment, actor: Superadmin, db: DbSession
) -> dict[str, object]:
    row, payment = db.get(Revenue, revenue_id), db.get(RevenuePayment, payment_id)
    if not row or not payment:
        raise not_found("Revenue payment")
    void_revenue_payment(db, row, payment, actor, payload.reason)
    db.commit()
    return revenue_get(row.id, actor, db)


def upload_proof(db: DbSession, entity: object, file: UploadFile, kind: str, actor: ActiveUser) -> dict[str, object]:
    before = model_snapshot(entity)
    old_path = getattr(entity, "proof_path", None)
    proof = proof_storage.upload(file, kind)
    try:
        entity.proof_path = proof["path"]
        entity.proof_url = proof["url"]
        audit(db, entity, "proof_replaced" if old_path else "proof_uploaded", actor, before=before)
        db.commit()
    except Exception:
        db.rollback()
        proof_storage.delete(proof["path"])
        raise
    if old_path and old_path != proof["path"]:
        proof_storage.delete(old_path)
    return {"data": {"proof_url": proof["url"]}}


@router.put("/operational-cashflows/{cashflow_id}/proof")
def operational_proof(
    cashflow_id: int, actor: ActiveUser, db: DbSession, proof: Annotated[UploadFile, File()]
) -> dict[str, object]:
    row = db.get(OperationalCashflow, cashflow_id)
    if not row:
        raise not_found("Operational cashflow")
    return upload_proof(db, row, proof, "operational-cashflow", actor)


@router.put("/operational-cashflows/{cashflow_id}/payments/{payment_id}/proof")
def operational_payment_proof(
    cashflow_id: int, payment_id: int, actor: ActiveUser, db: DbSession, proof: Annotated[UploadFile, File()]
) -> dict[str, object]:
    row = db.get(OperationalCashflowPayment, payment_id)
    if not row or row.operational_cashflow_id != cashflow_id:
        raise not_found("Operational payment")
    return upload_proof(db, row, proof, "operational-payments", actor)


@router.put("/revenues/{revenue_id}/proof")
def revenue_proof(
    revenue_id: int, actor: ActiveUser, db: DbSession, proof: Annotated[UploadFile, File()]
) -> dict[str, object]:
    row = db.get(Revenue, revenue_id)
    if not row:
        raise not_found("Revenue")
    return upload_proof(db, row, proof, "revenues", actor)


@router.put("/revenues/{revenue_id}/payments/{payment_id}/proof")
def revenue_payment_proof(
    revenue_id: int, payment_id: int, actor: ActiveUser, db: DbSession, proof: Annotated[UploadFile, File()]
) -> dict[str, object]:
    row = db.get(RevenuePayment, payment_id)
    if not row or row.revenue_id != revenue_id:
        raise not_found("Revenue payment")
    return upload_proof(db, row, proof, "revenue-payments", actor)
