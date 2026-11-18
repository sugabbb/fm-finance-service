from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, status
from pydantic import Field
from sqlalchemy import func, select

from app.core.decimal import money
from app.core.dependencies import DbSession, Superadmin
from app.core.errors import conflict, validation
from app.core.pagination import StrictModel
from app.db.models import (
    FinancialOpeningBalance,
    OperationalCashflow,
    OperationalCashflowPayment,
    Revenue,
    RevenuePayment,
)
from app.features.common import audit, model_snapshot

router = APIRouter(tags=["Settings"])


class FinancialOpeningInput(StrictModel):
    account: Literal["OPERATIONAL", "REVENUE"]
    amount_idr: Decimal = Field(ge=0)
    effective_date: date


@router.get("/settings/opening-balances")
def opening_balances(_: Superadmin, db: DbSession) -> dict[str, object]:
    rows = db.scalars(select(FinancialOpeningBalance).order_by(FinancialOpeningBalance.account)).all()
    return {"data": {"financial": [model_snapshot(row) for row in rows], "inventory": []}}


@router.post("/settings/financial-opening-balances", status_code=status.HTTP_201_CREATED)
def create_financial_opening(payload: FinancialOpeningInput, actor: Superadmin, db: DbSession) -> dict[str, object]:
    if db.scalar(select(FinancialOpeningBalance.id).where(FinancialOpeningBalance.account == payload.account)):
        raise conflict("OPENING_EXISTS", "An opening balance already exists for this account.")
    candidates = (
        [db.scalar(select(func.min(OperationalCashflow.paid_at))), db.scalar(select(func.min(OperationalCashflowPayment.paid_date)))]
        if payload.account == "OPERATIONAL"
        else [db.scalar(select(func.min(Revenue.paid_at))), db.scalar(select(func.min(RevenuePayment.paid_date)))]
    )
    first = min(filter(None, candidates), default=None)
    if first and payload.effective_date > (first.date() if hasattr(first, "date") else first):
        raise validation("effective_date", "Opening balance must start on or before the first payment.")
    row = FinancialOpeningBalance(
        account=payload.account, amount_idr=money(payload.amount_idr),
        effective_date=payload.effective_date, created_by=actor.id,
    )
    db.add(row)
    db.flush()
    audit(db, row, "financial_opening_created", actor)
    db.commit()
    return {"data": model_snapshot(row)}
