"""Read API of ``cash_statements``: the bank-statement view of the business (see
``cash_statement_service`` for how the rows are derived)."""

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import Row, Subquery, case, func, select

from app.core.decimal import decimal_string
from app.core.dependencies import ActiveUser, DbSession
from app.core.errors import validation
from app.core.pagination import PageDep
from app.db.models import CashStatement
from app.features.main_cash_account_service import CREDIT

router = APIRouter(tags=["Cash Statements"])

Source = Literal["operational_cashflows", "revenues", "main_cash_account_logs", "loans"]


def statement_query() -> Subquery:
    """Every statement row with the running ``balance`` after it (statement order:
    ``statement_date``, ``id``). The balance runs over the whole statement, so it stays the true
    running balance whatever filters are applied afterwards."""
    signed = case((CashStatement.direction == CREDIT, CashStatement.amount), else_=-CashStatement.amount)
    balance = func.sum(signed).over(order_by=(CashStatement.statement_date, CashStatement.id))
    return select(CashStatement, balance.label("balance")).subquery("statement")


def date_or_none(value: date | None) -> str | None:
    return value.isoformat() if value else None


def statement_out(row: Row[Any]) -> dict[str, object]:
    amount = decimal_string(row.amount, 0)
    is_credit = row.direction == CREDIT
    return {
        "id": row.id,
        "statement_date": row.statement_date.isoformat(),
        "description": row.description,
        "category": row.category,
        "counterparty": row.counterparty,
        "direction": row.direction,
        "amount": amount,
        "debit": None if is_credit else amount,
        "credit": amount if is_credit else None,
        "balance": decimal_string(row.balance, 0),
        "reference": row.reference,
        "source": {
            "table": row.source_table,
            "id": row.source_id,
            "line": row.source_line,
            "payment_id": row.payment_id,
            "payment_sequence": row.payment_sequence,
            "transaction_date": date_or_none(row.source_transaction_date),
            "amount": decimal_string(row.source_amount, 0),
            "payment_status": row.source_payment_status,
        },
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.get("/cash-statements")
def list_cash_statements(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    direction: Literal["credit", "debit"] | None = None,
    category: str | None = None,
    reference: str | None = Query(default=None, description="Every row of one transaction, e.g. revenues:7."),
    source_table: Source | None = None,
    source_id: int | None = Query(default=None, description="Only together with source_table."),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
) -> dict[str, object]:
    """Bank-statement style list, newest first, with debit/credit and the running balance."""
    if source_id is not None and source_table is None:
        raise validation("source_id", "source_id requires source_table.")
    statement = statement_query()
    stmt = select(statement)
    for column, value in (
        (statement.c.direction, direction),
        (statement.c.category, category),
        (statement.c.reference, reference),
        (statement.c.source_table, source_table),
        (statement.c.source_id, source_id),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if from_date:
        stmt = stmt.where(statement.c.statement_date >= from_date)
    if to_date:
        stmt = stmt.where(statement.c.statement_date <= to_date)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(statement.c.statement_date.desc(), statement.c.id.desc()).offset(page.offset).limit(page.per_page)
    ).all()
    return {"data": [statement_out(row) for row in rows], "meta": page.meta(total)}
