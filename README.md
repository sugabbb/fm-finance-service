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

## Monthly cut-off and potential revenue

The monthly cut-off (`GET /api/v1/monthly-cutoffs?year=&month=`) also reports the month's **potential revenue**: the month-end egg stock and fish weight estimate, valued at the month's average sales price per kg. The quantities come from operational-service's private month-end API; finance does the valuation. The contract, which operational-service has to implement, is in [`docs/operational-month-end-api.md`](docs/operational-month-end-api.md).

A month is closed only once operational has finalized both datasets; until then it answers `404` and the cut-off waits. Schedule the close hourly so it happens as soon as the data is available:

```bash
# crontab, server time = APP_TIMEZONE; replaces the former "0 0 1 * *" entry
5 * * * *  cd /srv/api && .venv/bin/python -m app.scripts.monthly_cutoff --pending
```

`--pending` closes the last ended month if it has no cut-off yet, and retries the Excel exports due (see below). Exit codes: `0` closed or already closed, `1` failed, `2` usage or month not ended, `3` waiting for operational data. Alert on `3` persisting for days, and on `1`. Restate a month after late entries or an operational correction with `--month YYYY-MM`.

## Monthly cut-off API

- `GET /api/v1/monthly-cutoffs/months?page=&per_page=&year=` is the list. It has every month from the first month with an operational cashflow up to the current month, newest first, whether it has a cut-off or not. Each row has:
  - `cutoff.status`: `NOT_ENDED`, `NOT_CREATED`, `QUEUED`, `RUNNING`, `WAITING_FOR_DATA`, `FAILED` or `CREATED`, with a `reason`, `can_generate` and the `last_job`.
  - Headline `figures`, all `null` without a cut-off (show them as `-`).
  - The Excel `export` status (`null` without a cut-off).
- `GET /api/v1/monthly-cutoffs?year=&month=`: the detail of one month (every figure), as before.
- `POST /api/v1/monthly-cutoffs/jobs` with `{"type": "CUTOFF" | "EXPORT", "year": 2026, "month": 7}`: the buttons of the list. `CUTOFF` creates or recreates the month's cut-off, then its Excel file; `EXPORT` (re)generates the Excel file. Only queues the job and answers `202` at once; the user refreshes the list to see it running and its outcome. Returns `409 JOB_IN_PROGRESS` if one is queued or running for the month, and `422` for a month that has not ended.
- `GET /api/v1/monthly-cutoffs/jobs/{id}`: one job (`QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED` with `reason_code`/`reason`).
- `GET /api/v1/monthly-cutoffs/export?year=&month=`: downloads the Excel workbook (`monthly-cutoff-YYYY-MM.xlsx`).

All are for ADMIN/SUPERADMIN.

### Background jobs: run the worker

The cut-off and Excel jobs run in a separate process, never in the API. Run the worker as a service next to the API:

```bash
python -m app.scripts.monthly_cutoff_worker            # keeps running, polls every 5 s
```

Several workers may run at once. Without one, the hourly `monthly_cutoff --pending` run picks up queued jobs, so they wait up to an hour instead of seconds. A job still running after `CUTOFF_JOB_TIMEOUT_MINUTES` (default 60) is marked interrupted (its worker died), and the month can be queued again. The hourly cron closes months through the same jobs: it never closes a month at the same time as a user's job (it takes over a queued one), and its "waiting for operational data" attempts show in the list.

## Monthly cut-off Excel export

Every close also produces an Excel workbook for the finance team. It has the same figures as the API: summary, profit and loss with its cash reconciliation, the month's cash statement with the running balance, totals by category, loans, receivables by age, and potential revenue.

- **Status** (`export` in the list and the detail): `NOT_STARTED`, `QUEUED`, `GENERATING`, `SUCCEEDED`, or `FAILED` with a `reason`. It also has `failed_attempts` (in a row), `automatic_retry` (whether the hourly run will try again), `can_generate` (whether the frontend may trigger it now), `last_attempt` (when, automatic or requested by whom) and `file` (the last successful workbook, kept when a regeneration fails).
- **Generation:** the first attempt runs right after the close (in the cut-off job). Every hourly `--pending` run then retries the exports of any month that are not started or failed, until `EXPORT_MAX_AUTOMATIC_ATTEMPTS` failures in a row (default 3). After that, a user queues it from the list (an `EXPORT` job, always allowed except while one is queued or running), or `python -m app.scripts.export_monthly_cutoff --month YYYY-MM` generates it. A generation still running after `EXPORT_GENERATION_TIMEOUT_MINUTES` (default 15) is reported as interrupted, counts as a failed attempt, and can be started again.
- **Storage** (`EXPORT_STORAGE_BACKEND`):
  - `local` (default): files under `EXPORT_STORAGE_DIR` (default `storage/exports`, relative to the working directory). On servers, use an absolute path on a disk that is backed up.
  - `gcs`: objects in the Google Cloud Storage bucket `EXPORT_GCS_BUCKET`, under `EXPORT_GCS_PREFIX`. Both default to `GOOGLE_CLOUD_STORAGE_BUCKET`/`GOOGLE_CLOUD_STORAGE_PREFIX`, the payment proofs' bucket; a separate private bucket is advisable for finance reports. Credentials: the key file `GOOGLE_APPLICATION_CREDENTIALS` (also read from `.env`), else the environment's default credentials. The service account needs `storage.objects.create`, `storage.objects.delete` (to replace a file) and `storage.objects.get` on the bucket, e.g. *Storage Object User*. The API refuses to start with `gcs` and no bucket. Objects stay private: users download through the API.
  - Every cut-off has its own file (`monthly-cutoffs/YYYY-MM/cutoff-<id>.xlsx`), so restating a month keeps the earlier workbook. Each file is recorded with its backend and location (`gs://bucket/object` for GCS) and always read back from there, so changing the backend only affects new files. To move old ones, regenerate them.
  - Local development against an emulator: `pip install gcp-storage-emulator`, then `gcp-storage-emulator start --port 9023 --default-bucket fm-finance-exports`, and set `STORAGE_EMULATOR_HOST=http://127.0.0.1:9023`. With it set, `tests/test_export_storage_gcs.py` also runs against the real client library.
- The workbook flags, on its first sheet, any cut-off warning. It also flags a cash statement that changed after the close; close the month again for figures that match.

## Checks

```bash
python -m pytest
ruff check app tests
mypy app
```
