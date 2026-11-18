# Farm Management Finance Service

Independent FastAPI/PostgreSQL service for customers, cashflows, revenues, payments, cash accounts, statements, lenders, loans, monthly cutoffs, and proof uploads. Non-financial farm operations and identity remain owned by `operational-service`.

## Setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
```

Use the same `JWT_SECRET`, `JWT_ISSUER`, and `JWT_AUDIENCE` as operational-service. Set `OPERATIONAL_INTERNAL_API_KEY` to operational-service's `INTERNAL_API_KEY`; this value is server-only and must never be shipped to the frontend.

The frontend logs in at operational-service and sends the returned Bearer token directly to either service. Finance validates the JWT locally, then calls operational-service's private authorization endpoint on every authenticated request so user deactivation, role changes, password-change requirements, and token-version revocation apply immediately. Finance has no login or user-management endpoint.

## Operational integration

Feed cashflows resolve feed types and salary cashflows resolve employees through operational private APIs. Finance stores the returned name/code snapshot, not an operational database foreign key.

Received feed and egg-sale inventory effects are committed to `operational_integration_outbox` in the same transaction as finance data. Deliver them with:

```bash
python -m app.scripts.operational_outbox_worker
python -m app.scripts.operational_outbox_worker --once
```

Delivery uses complete, revisioned, idempotent snapshots. Failed messages retry with exponential backoff; newer pending snapshots supersede older undelivered ones. See `../integration-notes-micro-service.md` for the full contract and operating procedures.

## Checks

```bash
python -m pytest
ruff check app tests
mypy app
```
