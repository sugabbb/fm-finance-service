from fastapi import APIRouter, Query, status
from pydantic import Field
from sqlalchemy import func, or_, select

from app.core.dependencies import ActiveUser, DbSession
from app.core.errors import not_found
from app.core.pagination import PageDep, StrictModel
from app.db.models import Customer
from app.features.common import audit, model_snapshot

router = APIRouter(tags=["Customers"])


class CustomerInput(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=1, max_length=50)
    customer_type: str = Field(pattern="^(WARUNG|AGEN)$")
    notes: str | None = Field(default=None, max_length=3000)


def customer_out(row: Customer) -> dict[str, object]:
    return model_snapshot(row)


@router.get("/customers")
def list_customers(
    _: ActiveUser, db: DbSession, page: PageDep, q: str | None = None,
    customer_type: str | None = Query(default=None, pattern="^(WARUNG|AGEN)$"),
) -> dict[str, object]:
    stmt = select(Customer)
    if q:
        stmt = stmt.where(or_(Customer.name.ilike(f"%{q}%"), Customer.phone.ilike(f"%{q}%")))
    if customer_type:
        stmt = stmt.where(Customer.customer_type == customer_type)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Customer.name, Customer.id).offset(page.offset).limit(page.per_page)).all()
    return {"data": [customer_out(row) for row in rows], "meta": page.meta(total)}


@router.get("/customers/{customer_id}")
def get_customer(customer_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(Customer, customer_id)
    if not row:
        raise not_found("Customer")
    return {"data": customer_out(row)}


@router.post("/customers", status_code=status.HTTP_201_CREATED)
def create_customer(payload: CustomerInput, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    row = Customer(**payload.model_dump())
    db.add(row)
    db.flush()
    audit(db, row, "customer_created", actor)
    db.commit()
    return {"data": customer_out(row)}


@router.patch("/customers/{customer_id}")
def update_customer(customer_id: int, payload: CustomerInput, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(Customer, customer_id)
    if not row:
        raise not_found("Customer")
    before = model_snapshot(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    audit(db, row, "customer_updated", actor, before=before)
    db.commit()
    return {"data": customer_out(row)}
