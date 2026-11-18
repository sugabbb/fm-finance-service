from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.decimal import line_amount, money, quantity
from app.core.errors import AppError, conflict, not_found, validation
from app.db.models import (
    Customer,
    FinancialOpeningBalance,
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
    RevenueEggDetail,
    RevenueFishDetail,
    RevenueOtherDetail,
    RevenuePayment,
    RevenueTransferDetail,
    User,
)
from app.features.cash_balance_service import CashflowState, sync_balances
from app.features.common import audit, model_snapshot, now_utc, today_local
from app.features.locks import lock_balances
from app.features.main_cash_account_service import RevenueState, enqueue_from_cashflow, enqueue_from_revenue
from app.features.operational_outbox import enqueue_egg_projection, enqueue_feed_projection
from app.integrations.operational import operational_client

OP_DETAIL_MODELS = (
    OperationalCashflowSalaryDetail,
    OperationalCashflowUtilityDetail,
    OperationalCashflowHealthDetail,
    OperationalCashflowOtherDetail,
    OperationalCashflowLivestockPurchaseDetail,
    OperationalCashflowBankAdminDetail,
    OperationalCashflowTopupDetail,
)

def at_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def payment_total(payments: list[Any]) -> Decimal:
    return sum((Decimal(row.amount_idr) for row in payments if row.voided_at is None), Decimal("0"))


def sync_payment_status(row: OperationalCashflow | Revenue) -> None:
    total = payment_total(row.payments)
    if total > Decimal(row.amount_idr):
        raise validation("amount_idr", "Total payments cannot exceed transaction amount.")
    active = [item for item in row.payments if item.voided_at is None]
    if not active:
        row.payment_status, row.paid_at, row.paid_by = "UNPAID", None, None
    elif total == Decimal(row.amount_idr):
        latest = max(active, key=lambda item: (item.paid_date, item.id or 0))
        row.payment_status, row.paid_at, row.paid_by = "PAID", at_start(latest.paid_date), latest.created_by
    else:
        row.payment_status, row.paid_at, row.paid_by = "PARTIALLY_PAID", None, None


def remaining(row: OperationalCashflow | Revenue) -> Decimal:
    if isinstance(row, OperationalCashflow) and row.direction == "INFLOW":
        paid = Decimal(row.amount_idr) if row.payment_status == "PAID" else Decimal("0")
    elif isinstance(row, Revenue) and (row.direction != "INFLOW" or row.payment_method != "CASH"):
        paid = Decimal(row.amount_idr) if row.payment_status == "PAID" else Decimal("0")
    else:
        paid = payment_total(row.payments)
    return Decimal(row.amount_idr) - paid


def normalize_feed_line(item: dict[str, Any], species: str, feed: dict[str, Any]) -> dict[str, Any]:
    if feed["species"] != species or not feed["is_active"]:
        raise validation("details.feed_type_id", "Feed type does not match the cashflow species.")
    unit = item["purchase_unit"]
    purchase_qty = quantity(item["purchase_quantity"])
    purchase_price = quantity(item["price_per_purchase_unit_idr"])
    kg_per_sak = quantity(item["kg_per_sak"]) if item.get("kg_per_sak") is not None else None
    if unit == "SAK":
        quantity_kg = quantity(purchase_qty * kg_per_sak)
        price_per_kg = quantity(purchase_price / kg_per_sak)
    else:
        quantity_kg, price_per_kg, kg_per_sak = purchase_qty, purchase_price, None
    return {
        "feed_type_id": feed["id"],
        "feed_type_code": feed["code"],
        "feed_type_name": feed["name"],
        "feed_species": feed["species"],
        "purchase_unit": unit,
        "purchase_quantity": purchase_qty,
        "kg_per_sak": kg_per_sak,
        "price_per_purchase_unit_idr": purchase_price,
        "quantity_kg": quantity_kg,
        "price_per_kg_idr": price_per_kg,
        "line_amount_idr": line_amount(purchase_qty, purchase_price),
    }


def clear_operational_details(db: Session, row_id: int) -> None:
    db.execute(
        delete(OperationalCashflowFeedDetail).where(OperationalCashflowFeedDetail.operational_cashflow_id == row_id)
    )
    for model in OP_DETAIL_MODELS:
        db.execute(delete(model).where(model.operational_cashflow_id == row_id))


def save_operational(
    db: Session,
    data: dict[str, Any],
    actor: User,
    row: OperationalCashflow | None = None,
    *,
    correction: bool = False,
    reason: str | None = None,
) -> OperationalCashflow:
    lock_balances(db)
    if row:
        row = db.scalar(
            select(OperationalCashflow)
            .where(OperationalCashflow.id == row.id)
            .options(selectinload(OperationalCashflow.payments), selectinload(OperationalCashflow.feed_details))
            .with_for_update()
        )
        if not row:
            raise not_found("Operational cashflow")
        if correction and actor.role != "SUPERADMIN":
            raise AppError(403, "FORBIDDEN", "Only Superadmin can correct transactions.")
        if not correction and (row.payment_status == "PAID" or any(not item.voided_at for item in row.payments)):
            raise conflict("CORRECTION_REQUIRED", "A transaction with payments can only be corrected by Superadmin.")
        if not correction and any(line.received_at for line in row.feed_details):
            raise conflict("CORRECTION_REQUIRED", "Received feed lines can only be corrected by Superadmin.")
        if not correction and row.approval_status == "SUBMITTED":
            raise conflict("TRANSACTION_LOCKED", "A submitted transaction cannot be edited.")
    before = model_snapshot(row) if row else None
    balances_before = CashflowState.of(row) if row else None
    cashflow_type = data["cashflow_type"]
    direction = "INFLOW" if cashflow_type == "MAIN_ACCOUNT_TOPUP" else "OUTFLOW"
    if row and row.direction != direction:
        raise conflict("DIRECTION_IMMUTABLE", "Cashflow direction cannot be changed.")
    details = data["details"]
    normalized_lines: list[dict[str, Any]] = []
    if cashflow_type in ("FEED_AYAM", "FEED_LELE"):
        species = "AYAM" if cashflow_type == "FEED_AYAM" else "LELE"
        feeds = operational_client.resolve_feed_types([item["feed_type_id"] for item in details])
        normalized_lines = [normalize_feed_line(item, species, feeds[item["feed_type_id"]]) for item in details]
        amount = sum((line["line_amount_idr"] for line in normalized_lines), Decimal("0"))
    else:
        amount = money(data["amount_idr"])
    if row and amount < payment_total(row.payments):
        raise validation("amount_idr", "Transaction amount cannot be below recorded payments.")
    creating = row is None
    if row is None:
        row = OperationalCashflow(created_by=actor.id, payment_status="UNPAID")
        db.add(row)
    old_status = row.approval_status if row.id else None
    status_value = "NOT_REQUIRED" if direction == "INFLOW" else (row.approval_status if row.id else "DRAFT")
    if row.id and direction == "OUTFLOW" and old_status == "APPROVED" and amount > Decimal(row.amount_idr):
        status_value = "DRAFT"
        row.submitted_at = row.approved_at = row.rejected_at = None
        row.submitted_by = row.approved_by = row.rejected_by = None
    row.cashflow_type, row.direction, row.transaction_date, row.amount_idr = (
        cashflow_type,
        direction,
        data["transaction_date"],
        amount,
    )
    row.approval_status, row.rejection_reason = status_value, None
    db.flush()
    old_receipts = {line.feed_type_id: (line.received_at, line.received_by) for line in row.feed_details}
    clear_operational_details(db, row.id)
    if normalized_lines:
        for line in normalized_lines:
            received_at, received_by = old_receipts.get(line["feed_type_id"], (None, None))
            db.add(
                OperationalCashflowFeedDetail(
                    operational_cashflow_id=row.id, received_at=received_at, received_by=received_by, **line
                )
            )
    elif cashflow_type == "SALARY":
        employee = operational_client.employee(details["employee_id"])
        if creating and employee["status"] != "ACTIVE":
            raise validation("details.employee_id", "Employee must exist and be active for a new salary transaction.")
        db.add(
            OperationalCashflowSalaryDetail(
                operational_cashflow_id=row.id, employee_name=employee["name"], **details
            )
        )
    elif cashflow_type == "UTILITY":
        db.add(OperationalCashflowUtilityDetail(operational_cashflow_id=row.id, **details))
    elif cashflow_type == "HEALTH":
        db.add(OperationalCashflowHealthDetail(operational_cashflow_id=row.id, **details))
    elif cashflow_type == "OTHER":
        db.add(OperationalCashflowOtherDetail(operational_cashflow_id=row.id, **details))
    elif cashflow_type in ("LELE_SEED_PURCHASE", "PULLET_PURCHASE"):
        db.add(
            OperationalCashflowLivestockPurchaseDetail(
                operational_cashflow_id=row.id,
                species="LELE" if cashflow_type == "LELE_SEED_PURCHASE" else "AYAM",
                **details,
            )
        )
    elif cashflow_type == "BANK_ADMIN_FEE":
        db.add(OperationalCashflowBankAdminDetail(operational_cashflow_id=row.id, **details))
    else:
        db.add(OperationalCashflowTopupDetail(operational_cashflow_id=row.id, **details))
    if creating and direction == "INFLOW":
        row.payment_status, row.paid_at, row.paid_by = (
            "PAID",
            at_start(min(row.transaction_date, today_local())),
            actor.id,
        )
    elif row.payments:
        sync_payment_status(row)
    action = "cashflow_corrected" if correction else ("cashflow_updated" if before else "cashflow_created")
    audit(db, row, action, actor, before=before, reason=reason)
    sync_balances(db, balances_before, row)
    enqueue_from_cashflow(db, balances_before, row, action)
    enqueue_feed_projection(db, row)
    return row


def submit_operational(db: Session, row: OperationalCashflow, actor: User) -> OperationalCashflow:
    lock_balances(db)
    row = db.scalar(select(OperationalCashflow).where(OperationalCashflow.id == row.id).with_for_update())
    if row.direction != "OUTFLOW" or row.approval_status not in ("DRAFT", "REJECTED"):
        raise conflict("INVALID_STATE", "Cashflow cannot be submitted in its current state.")
    before = model_snapshot(row)
    balances_before = CashflowState.of(row)
    row.approval_status, row.submitted_by, row.submitted_at, row.rejection_reason = (
        "SUBMITTED",
        actor.id,
        now_utc(),
        None,
    )
    audit(db, row, "cashflow_submitted", actor, before=before)
    sync_balances(db, balances_before, row)
    return row


def decide_operational(
    db: Session, row: OperationalCashflow, actor: User, decision: str, reason: str | None
) -> OperationalCashflow:
    lock_balances(db)
    row = db.scalar(select(OperationalCashflow).where(OperationalCashflow.id == row.id).with_for_update())
    if row.direction != "OUTFLOW" or row.approval_status != "SUBMITTED":
        raise conflict("INVALID_STATE", "Only a submitted outflow can be decided.")
    before = model_snapshot(row)
    balances_before = CashflowState.of(row)
    if decision == "APPROVED":
        row.approval_status, row.approved_by, row.approved_at = "APPROVED", actor.id, now_utc()
        row.rejected_by = row.rejected_at = row.rejection_reason = None
    else:
        row.approval_status, row.rejected_by, row.rejected_at, row.rejection_reason = (
            "REJECTED",
            actor.id,
            now_utc(),
            reason,
        )
        row.approved_by = row.approved_at = None
    audit(db, row, decision.lower(), actor, before=before, reason=reason)
    sync_balances(db, balances_before, row)
    enqueue_feed_projection(db, row)
    return row


def payment_amount(mode: str, requested: Decimal | None, remainder: Decimal) -> Decimal:
    if mode == "FULL":
        return remainder
    value = money(requested or 0)
    if value <= 0 or value >= remainder:
        raise validation("amount_idr", "Partial payment must be positive and less than the remaining amount.")
    return value


def pay_operational(
    db: Session, row: OperationalCashflow, actor: User, paid_date: date, mode: str, amount: Decimal | None
) -> OperationalCashflow:
    lock_balances(db)
    row = db.scalar(
        select(OperationalCashflow)
        .where(OperationalCashflow.id == row.id)
        .options(selectinload(OperationalCashflow.payments))
        .with_for_update()
    )
    if paid_date > today_local():
        raise validation("paid_date", "Payment date cannot be in the future.")
    if row.direction != "OUTFLOW" or row.approval_status != "APPROVED":
        raise conflict("INVALID_STATE", "Only an approved outflow can be paid.")
    if row.payment_status == "PAID":
        return row
    before = model_snapshot(row)
    balances_before = CashflowState.of(row)
    value = payment_amount(mode, amount, remaining(row))
    db.add(
        OperationalCashflowPayment(
            operational_cashflow_id=row.id,
            paid_date=paid_date,
            amount_idr=value,
            payment_mode=mode,
            created_by=actor.id,
        )
    )
    db.flush()
    db.refresh(row, attribute_names=["payments"])
    sync_payment_status(row)
    audit(
        db,
        row,
        "cashflow_paid" if row.payment_status == "PAID" else "cashflow_partially_paid",
        actor,
        before=before,
        after=model_snapshot(row) | {"payment_amount_idr": str(value), "payment_date": paid_date.isoformat()},
    )
    sync_balances(db, balances_before, row)
    return row


def void_operational_payment(
    db: Session, row: OperationalCashflow, payment: OperationalCashflowPayment, actor: User, reason: str
) -> OperationalCashflow:
    lock_balances(db)
    row = db.scalar(
        select(OperationalCashflow)
        .where(OperationalCashflow.id == row.id)
        .options(selectinload(OperationalCashflow.payments))
        .with_for_update()
    )
    payment = db.scalar(
        select(OperationalCashflowPayment).where(OperationalCashflowPayment.id == payment.id).with_for_update()
    )
    if not payment or payment.operational_cashflow_id != row.id:
        raise not_found("Operational payment")
    if payment.voided_at:
        return row
    before = model_snapshot(row)
    balances_before = CashflowState.of(row)
    payment.voided_at, payment.voided_by, payment.void_reason = now_utc(), actor.id, reason
    sync_payment_status(row)
    audit(db, row, "cashflow_payment_voided", actor, before=before, reason=reason)
    sync_balances(db, balances_before, row)
    return row


def clear_revenue_details(db: Session, row_id: int) -> None:
    for model in (RevenueEggDetail, RevenueFishDetail, RevenueOtherDetail, RevenueTransferDetail):
        db.execute(delete(model).where(model.revenue_id == row_id))


def save_revenue(
    db: Session,
    data: dict[str, Any],
    actor: User,
    row: Revenue | None = None,
    *,
    correction: bool = False,
    reason: str | None = None,
) -> Revenue:
    lock_balances(db)
    if row:
        row = db.scalar(
            select(Revenue)
            .where(Revenue.id == row.id)
            .options(selectinload(Revenue.payments), selectinload(Revenue.egg_details))
            .with_for_update()
        )
        if not row:
            raise not_found("Revenue")
        if correction and actor.role != "SUPERADMIN":
            raise AppError(403, "FORBIDDEN", "Only Superadmin can correct transactions.")
        if not correction and (row.payment_status == "PAID" or any(not item.voided_at for item in row.payments)):
            raise conflict("CORRECTION_REQUIRED", "A transaction with payments can only be corrected by Superadmin.")
        if not correction and row.approval_status == "SUBMITTED":
            raise conflict("TRANSACTION_LOCKED", "A submitted transaction cannot be edited.")
    before = model_snapshot(row) if row else None
    main_account_before = RevenueState.of(row) if row else None
    revenue_type = data["revenue_type"]
    direction = "OUTFLOW" if revenue_type == "MAIN_ACCOUNT_TRANSFER" else "INFLOW"
    if row and row.direction != direction:
        raise conflict("DIRECTION_IMMUTABLE", "Revenue direction cannot be changed.")
    details = data["details"]
    customer_id = None
    if revenue_type in ("EGG_SALE", "FISH_SALE"):
        if data.get("new_customer"):
            customer = Customer(**data["new_customer"])
            db.add(customer)
            db.flush()
            audit(db, customer, "customer_created", actor)
            customer_id = customer.id
        else:
            if not db.get(Customer, data.get("customer_id")):
                raise validation("customer_id", "Customer does not exist.")
            customer_id = data["customer_id"]
    if revenue_type == "EGG_SALE":
        categories = [item["egg_category"] for item in details]
        if len(categories) != len(set(categories)):
            raise validation("details", "Egg categories cannot be duplicated.")
        amount = sum((line_amount(item["weight_kg"], item["price_per_kg_idr"]) for item in details), Decimal("0"))
    elif revenue_type == "FISH_SALE":
        amount = line_amount(details["weight_kg"], details["price_per_kg_idr"])
    elif revenue_type == "MAIN_ACCOUNT_TRANSFER":
        gross, cost = money(details["gross_amount_idr"]), money(details["payment_cost_idr"])
        amount = gross - cost
    else:
        amount = money(data["amount_idr"])
    payment_method = data.get("payment_method") if direction == "INFLOW" else None
    direct_date = data.get("direct_transfer_date") if payment_method == "DIRECT_TRANSFER" else None
    if direct_date and direct_date > today_local():
        raise validation("direct_transfer_date", "Direct transfer date cannot be in the future.")
    if row and row.payment_method == "CASH" and amount < payment_total(row.payments):
        raise validation("amount_idr", "Revenue amount cannot be below recorded payments.")
    if row is None:
        row = Revenue(created_by=actor.id, payment_status="UNPAID")
        db.add(row)
    if direction == "OUTFLOW":
        approval = row.approval_status if row.id else "DRAFT"
    elif payment_method == "DIRECT_TRANSFER":
        approval = "APPROVED" if correction and row.payment_status == "PAID" else "SUBMITTED"
    else:
        approval = "NOT_REQUIRED"
    row.revenue_type, row.direction, row.transaction_date, row.amount_idr = (
        revenue_type,
        direction,
        data["transaction_date"],
        amount,
    )
    row.customer_id, row.payment_method, row.direct_transfer_date = customer_id, payment_method, direct_date
    row.approval_status, row.rejection_reason = approval, None
    if approval == "SUBMITTED":
        row.submitted_at, row.submitted_by = now_utc(), actor.id
    db.flush()
    clear_revenue_details(db, row.id)
    if revenue_type == "EGG_SALE":
        for item in details:
            db.add(
                RevenueEggDetail(
                    revenue_id=row.id,
                    egg_category=item["egg_category"],
                    weight_kg=quantity(item["weight_kg"]),
                    quantity_pieces=item["quantity_pieces"],
                    price_per_kg_idr=quantity(item["price_per_kg_idr"]),
                    line_amount_idr=line_amount(item["weight_kg"], item["price_per_kg_idr"]),
                    notes=item.get("notes"),
                )
            )
    elif revenue_type == "FISH_SALE":
        db.add(
            RevenueFishDetail(
                revenue_id=row.id,
                weight_kg=quantity(details["weight_kg"]),
                price_per_kg_idr=quantity(details["price_per_kg_idr"]),
                notes=details.get("notes"),
            )
        )
    elif revenue_type == "MAIN_ACCOUNT_TRANSFER":
        db.add(
            RevenueTransferDetail(
                revenue_id=row.id, gross_amount_idr=gross, payment_cost_idr=cost, notes=details.get("notes")
            )
        )
    else:
        db.add(RevenueOtherDetail(revenue_id=row.id, notes=details.get("notes")))
    if row.payments and payment_method == "CASH":
        sync_payment_status(row)
    action = "revenue_corrected" if correction else ("revenue_updated" if before else "revenue_created")
    audit(db, row, action, actor, before=before, reason=reason)
    enqueue_from_revenue(db, main_account_before, row, action)
    enqueue_egg_projection(db, row)
    return row


def submit_revenue(db: Session, row: Revenue, actor: User) -> Revenue:
    row = db.scalar(select(Revenue).where(Revenue.id == row.id).with_for_update())
    if row.direction != "OUTFLOW" or row.approval_status not in ("DRAFT", "REJECTED"):
        raise conflict("INVALID_STATE", "Revenue transfer cannot be submitted in its current state.")
    before = model_snapshot(row)
    row.approval_status, row.submitted_at, row.submitted_by, row.rejection_reason = (
        "SUBMITTED",
        now_utc(),
        actor.id,
        None,
    )
    audit(db, row, "transfer_submitted", actor, before=before)
    return row


def revenue_cash(db: Session, as_of: date) -> Decimal:
    opening = (
        db.scalar(
            select(func.coalesce(func.sum(FinancialOpeningBalance.amount_idr), 0)).where(
                FinancialOpeningBalance.account == "REVENUE", FinancialOpeningBalance.effective_date <= as_of
            )
        )
        or 0
    )
    income = (
        db.scalar(
            select(func.coalesce(func.sum(RevenuePayment.amount_idr), 0)).where(
                RevenuePayment.voided_at.is_(None), RevenuePayment.paid_date <= as_of
            )
        )
        or 0
    )
    outflow = (
        db.scalar(
            select(func.coalesce(func.sum(RevenueTransferDetail.gross_amount_idr), 0))
            .join(Revenue)
            .where(
                Revenue.direction == "OUTFLOW", Revenue.payment_status == "PAID", func.date(Revenue.paid_at) <= as_of
            )
        )
        or 0
    )
    return Decimal(opening) + Decimal(income) - Decimal(outflow)


def guard_revenue_negative(
    db: Session, effective_date: date, gross: Decimal, acknowledge: bool, row: Revenue, actor: User
) -> None:
    dates = {effective_date, today_local()}
    dates.update(
        db.scalars(
            select(RevenuePayment.paid_date).where(
                RevenuePayment.paid_date >= effective_date, RevenuePayment.voided_at.is_(None)
            )
        ).all()
    )
    dates.update(
        value.date()
        for value in db.scalars(
            select(Revenue.paid_at).where(Revenue.paid_at.is_not(None), func.date(Revenue.paid_at) >= effective_date)
        ).all()
    )
    for check_date in sorted(dates):
        projected = revenue_cash(db, check_date) - gross
        if projected < 0:
            if not acknowledge:
                raise AppError(
                    409,
                    "NEGATIVE_BALANCE_CONFIRMATION_REQUIRED",
                    "Revenue balance would become negative.",
                    extra={"projected_balance": str(projected), "earliest_negative_date": check_date.isoformat()},
                )
            audit(
                db,
                row,
                "negative_balance_override",
                actor,
                reason=f"REVENUE {check_date}: {projected}",
                after={
                    "account": "REVENUE",
                    "effective_date": effective_date.isoformat(),
                    "earliest_negative_date": check_date.isoformat(),
                    "projected_balance": str(projected),
                    "acknowledged_by": actor.id,
                },
            )
            return


def decide_revenue(
    db: Session, row: Revenue, actor: User, decision: str, reason: str | None, acknowledge: bool
) -> Revenue:
    lock_balances(db)
    row = db.scalar(select(Revenue).where(Revenue.id == row.id).with_for_update())
    direct = row.direction == "INFLOW" and row.payment_method == "DIRECT_TRANSFER"
    if row.approval_status != "SUBMITTED" or (not direct and row.direction != "OUTFLOW"):
        raise conflict("INVALID_STATE", "Only a submitted transfer can be decided.")
    before = model_snapshot(row)
    main_account_before = RevenueState.of(row)
    if decision == "REJECTED":
        row.approval_status, row.rejected_at, row.rejected_by, row.rejection_reason = (
            "REJECTED",
            now_utc(),
            actor.id,
            reason,
        )
        row.approved_at = row.approved_by = None
        audit(
            db, row, "direct_transfer_rejected" if direct else "transfer_rejected", actor, before=before, reason=reason
        )
        enqueue_egg_projection(db, row)
        return row
    if direct:
        paid_date = row.direct_transfer_date
    else:
        detail = db.scalar(select(RevenueTransferDetail).where(RevenueTransferDetail.revenue_id == row.id))
        guard_revenue_negative(db, today_local(), Decimal(detail.gross_amount_idr), acknowledge, row, actor)
        paid_date = today_local()
    row.approval_status, row.approved_at, row.approved_by = "APPROVED", now_utc(), actor.id
    row.rejected_at = row.rejected_by = row.rejection_reason = None
    row.payment_status, row.paid_at, row.paid_by = "PAID", at_start(paid_date), actor.id
    action = "direct_transfer_approved" if direct else "transfer_approved"
    audit(db, row, action, actor, before=before)
    enqueue_from_revenue(db, main_account_before, row, action)
    enqueue_egg_projection(db, row)
    return row


def pay_revenue(db: Session, row: Revenue, actor: User, paid_date: date, mode: str, amount: Decimal | None) -> Revenue:
    lock_balances(db)
    row = db.scalar(
        select(Revenue).where(Revenue.id == row.id).options(selectinload(Revenue.payments)).with_for_update()
    )
    if paid_date > today_local():
        raise validation("paid_date", "Payment date cannot be in the future.")
    if row.direction != "INFLOW" or row.payment_method != "CASH":
        raise conflict("INVALID_STATE", "Only cash inflow revenue can be paid manually.")
    if row.payment_status == "PAID":
        return row
    before = model_snapshot(row)
    value = payment_amount(mode, amount, remaining(row))
    db.add(
        RevenuePayment(revenue_id=row.id, paid_date=paid_date, amount_idr=value, payment_mode=mode, created_by=actor.id)
    )
    db.flush()
    db.refresh(row, attribute_names=["payments"])
    sync_payment_status(row)
    audit(
        db,
        row,
        "revenue_paid" if row.payment_status == "PAID" else "revenue_partially_paid",
        actor,
        before=before,
        after=model_snapshot(row) | {"payment_amount_idr": str(value), "payment_date": paid_date.isoformat()},
    )
    return row


def void_revenue_payment(db: Session, row: Revenue, payment: RevenuePayment, actor: User, reason: str) -> Revenue:
    lock_balances(db)
    row = db.scalar(
        select(Revenue).where(Revenue.id == row.id).options(selectinload(Revenue.payments)).with_for_update()
    )
    payment = db.scalar(select(RevenuePayment).where(RevenuePayment.id == payment.id).with_for_update())
    if not payment or payment.revenue_id != row.id:
        raise not_found("Revenue payment")
    if payment.voided_at:
        return row
    before = model_snapshot(row)
    payment.voided_at, payment.voided_by, payment.void_reason = now_utc(), actor.id, reason
    sync_payment_status(row)
    audit(db, row, "revenue_payment_voided", actor, before=before, reason=reason)
    return row
