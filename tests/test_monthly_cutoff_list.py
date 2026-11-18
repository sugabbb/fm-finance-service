"""Checks for the list of months (GET /monthly-cutoffs/months) and the background jobs that create
cut-offs and Excel files (POST /monthly-cutoffs/jobs, the worker, the hourly cron).

Built on the month of test_monthly_cutoff (``month`` fixture): its first operational cashflow is in
``OLDER``, so the list is CURRENT (not ended), MONTH and OLDER.
"""

from datetime import date, timedelta

import pytest
import test_monthly_cutoff as scenario
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session
from test_monthly_cutoff import CURRENT, MONTH, OLDER, month_params

from app.core.config import settings
from app.db.models import MonthlyCutoff, MonthlyCutoffExport, MonthlyCutoffJob, User
from app.features import monthly_cutoff_export, monthly_cutoff_service
from app.features.cash_statement_service import Period
from app.features.common import now_utc
from app.features.monthly_cutoff_export import due_for_automatic_attempt, generate_export, retry_exports
from app.features.monthly_cutoff_jobs import run_queued
from app.features.monthly_cutoff_service import run_cutoff
from app.scripts import monthly_cutoff, monthly_cutoff_worker

month = scenario.month
client = scenario.client

pytestmark = pytest.mark.usefixtures("operational_month_end")

LIST_URL = "/api/v1/monthly-cutoffs/months"
JOBS_URL = "/api/v1/monthly-cutoffs/jobs"
EXPORT_URL = "/api/v1/monthly-cutoffs/export"
EARLY = Period(date(MONTH.start.year - 2, MONTH.start.month, 1))  # a month without activity
WORKING = monthly_cutoff_export.build_workbook  # what generations use, once repaired


def broken(*args: object, **kwargs: object) -> bytes:
    """A generation that fails (installed with monkeypatch in place of build_workbook)."""
    raise OSError("disk full")


def listed(client: TestClient, **params: object) -> dict:
    response = client.get(LIST_URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def item(client: TestClient, period: Period) -> dict:
    for row in listed(client, per_page=100)["data"]:
        if row["period"]["start"] == period.start.isoformat():
            return row
    raise AssertionError(f"{period.label} is not listed")


def queue(client: TestClient, job_type: str, period: Period) -> dict:
    body = {"type": job_type, "year": period.start.year, "month": period.start.month}
    return client.post(JOBS_URL, json=body).json()


def jobs(db: Session) -> list[MonthlyCutoffJob]:
    db.expire_all()
    return list(db.scalars(select(MonthlyCutoffJob).order_by(MonthlyCutoffJob.id)).all())


# --- The list: every month ----------------------------------------------------------------------


def test_every_month_from_the_first_cashflow_whether_closed_or_not(
    db: Session, month: dict[str, int], client: TestClient
) -> None:
    run_cutoff(period=MONTH, session=db)
    generate_export(MONTH, session=db)

    body = listed(client)

    assert body["meta"] == {"page": 1, "per_page": 30, "total": 3, "pages": 1}
    current, closed, older = body["data"]
    assert [row["period"]["start"] for row in body["data"]] == [
        CURRENT.start.isoformat(),
        MONTH.start.isoformat(),
        OLDER.start.isoformat(),
    ], "from the current month back to the first operational cashflow's"

    nothing = dict.fromkeys(
        ("cash_closing", "revenue", "net_profit", "receivables", "loan_principal_outstanding", "potential_revenue")
    )
    assert older == {
        "period": {
            "year": OLDER.start.year,
            "month": OLDER.start.month,
            "start": OLDER.start.isoformat(),
            "end": OLDER.end.isoformat(),
        },
        "cutoff": {
            "status": "NOT_CREATED",
            "reason": "The cut-off of this month has not been created yet.",
            "can_generate": True,
            "id": None,
            "revision": None,
            "generated_at": None,
            "warning_count": None,
            "last_job": None,
        },
        "figures": nothing,
        "export": None,
    }, "listed without a cut-off, every figure empty"
    assert (current["cutoff"]["status"], current["cutoff"]["can_generate"]) == ("NOT_ENDED", False)
    assert (current["figures"], current["export"]) == (nothing, None)

    assert (closed["cutoff"]["status"], closed["cutoff"]["revision"], closed["cutoff"]["can_generate"]) == (
        "CREATED",
        1,
        True,
    )
    assert closed["figures"] == {
        "cash_closing": "5416000",
        "revenue": "175000",
        "net_profit": "-211500",
        "receivables": "650000",
        "loan_principal_outstanding": "2530000",
        "potential_revenue": "500000",
    }
    assert (closed["export"]["status"], closed["export"]["can_generate"]) == ("SUCCEEDED", True)
    assert closed["export"]["file"]["file_name"] == f"monthly-cutoff-{MONTH.label}.xlsx"
    # The detail has the same export object.
    assert client.get(scenario.URL, params=month_params()).json()["data"]["export"] == closed["export"]


def test_an_earlier_cut_off_extends_the_list_and_pages(db: Session, month: dict[str, int], client: TestClient) -> None:
    run_cutoff(period=EARLY, session=db)  # before the first cashflow: still listed, and every month since

    body = listed(client, per_page=10)

    months_between = (CURRENT.start.year - EARLY.start.year) * 12 + CURRENT.start.month - EARLY.start.month + 1
    assert body["meta"]["total"] == months_between
    last = listed(client, per_page=10, page=body["meta"]["pages"])["data"][-1]
    assert (last["period"]["start"], last["cutoff"]["status"]) == (EARLY.start.isoformat(), "CREATED")
    in_year = listed(client, year=EARLY.start.year, per_page=100)["data"]
    assert [row["period"]["month"] for row in in_year] == list(range(12, EARLY.start.month - 1, -1))
    assert listed(client, year=2001)["data"] == []


def test_nothing_to_list_and_access(db: Session, actor: User, client: TestClient) -> None:
    assert listed(client) == {"data": [], "meta": {"page": 1, "per_page": 30, "total": 0, "pages": 0}}
    assert client.get(LIST_URL, headers={"Authorization": ""}).status_code == 401
    assert client.get(LIST_URL, params={"year": 1999}).status_code == 422
    db.execute(update(User).where(User.id == actor.id).values(role="ADMIN"))
    db.commit()
    assert client.get(LIST_URL).status_code == 200


# --- Creating a cut-off from the list -----------------------------------------------------------


def test_a_cut_off_queued_from_the_list_runs_in_the_background(
    db: Session, month: dict[str, int], client: TestClient
) -> None:
    response = client.post(JOBS_URL, json={"type": "CUTOFF", "year": OLDER.start.year, "month": OLDER.start.month})

    assert response.status_code == 202
    job = response.json()["data"]
    assert {key: job[key] for key in ("type", "status", "reason_code", "reason", "started_at", "cutoff_id")} == {
        "type": "CUTOFF",
        "status": "QUEUED",
        "reason_code": None,
        "reason": None,
        "started_at": None,
        "cutoff_id": None,
    }
    assert job["requested_by"]["name"] == "Finance Admin"
    assert db.scalar(select(func.count()).select_from(MonthlyCutoff)) == 0, "the API computed nothing"
    row = item(client, OLDER)
    assert (row["cutoff"]["status"], row["cutoff"]["can_generate"], row["cutoff"]["last_job"]["id"]) == (
        "QUEUED",
        False,
        job["id"],
    )
    again = client.post(JOBS_URL, json={"type": "CUTOFF", "year": OLDER.start.year, "month": OLDER.start.month})
    assert (again.status_code, again.json()["code"], again.json()["job"]["id"]) == (409, "JOB_IN_PROGRESS", job["id"])

    (result,) = run_queued(session=db)  # the worker

    assert result.status == "SUCCEEDED"
    row = item(client, OLDER)
    assert (row["cutoff"]["status"], row["cutoff"]["revision"], row["cutoff"]["last_job"]["status"]) == (
        "CREATED",
        1,
        "SUCCEEDED",
    )
    assert row["cutoff"]["last_job"]["cutoff_id"] == row["cutoff"]["id"] is not None
    assert row["figures"]["cash_closing"] is not None
    assert row["export"]["status"] == "SUCCEEDED", "the Excel file follows the cut-off"
    assert client.get(f"{JOBS_URL}/{job['id']}").json()["data"]["status"] == "SUCCEEDED"

    # Regenerate: a new cut-off of the month (revision 2).
    assert queue(client, "CUTOFF", OLDER)["data"]["status"] == "QUEUED"
    run_queued(session=db)
    assert item(client, OLDER)["cutoff"]["revision"] == 2


def test_a_cut_off_that_cannot_be_created_says_why(
    db: Session,
    month: dict[str, int],
    client: TestClient,
    operational_month_end: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operational_month_end.missing = {"egg-stock"}
    queue(client, "CUTOFF", MONTH)
    run_queued(session=db)

    row = item(client, MONTH)
    assert (row["cutoff"]["status"], row["cutoff"]["can_generate"], row["cutoff"]["id"]) == (
        "WAITING_FOR_DATA",
        True,
        None,
    )
    assert row["cutoff"]["reason"] == (
        f"operational-service has not published the month-end data of {MONTH.label} yet (egg stock); "
        "the cut-off is created once it has."
    )
    assert row["cutoff"]["last_job"]["reason_code"] == "OPERATIONAL_DATA_NOT_READY"

    # Data available, the month closed; then recreating it fails: the earlier cut-off stays reported.
    operational_month_end.missing = set()
    queue(client, "CUTOFF", MONTH)
    run_queued(session=db)
    closed = item(client, MONTH)
    assert closed["cutoff"]["status"] == "CREATED"

    def boom(*args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(monthly_cutoff_service, "compute_cutoff", boom)
    queue(client, "CUTOFF", MONTH)
    run_queued(session=db)

    row = item(client, MONTH)
    assert (row["cutoff"]["status"], row["cutoff"]["reason"]) == ("FAILED", "RuntimeError: boom")
    assert (row["cutoff"]["id"], row["figures"]) == (closed["cutoff"]["id"], closed["figures"])
    assert row["cutoff"]["last_job"]["reason_code"] == "ERROR"


def test_queue_validation(db: Session, month: dict[str, int], client: TestClient) -> None:
    response = client.post(JOBS_URL, json={"type": "CUTOFF", "year": CURRENT.start.year, "month": CURRENT.start.month})
    assert (response.status_code, list(response.json()["errors"])) == (422, ["month"]), "not ended"
    response = client.post(JOBS_URL, json={"type": "EXPORT", "year": MONTH.start.year, "month": MONTH.start.month})
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND"), "nothing to export"
    for body in ({"type": "OTHER", "year": 2026, "month": 1}, {"type": "CUTOFF", "year": 2026, "month": 13}, {}):
        assert client.post(JOBS_URL, json=body).status_code == 422
    assert client.post(JOBS_URL, json={"type": "CUTOFF", "year": 2026, "month": 1}, headers={"Authorization": ""}).status_code == 401
    assert client.get(f"{JOBS_URL}/999999").status_code == 404
    assert jobs(db) == []

    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths[JOBS_URL]) == {"post"} and set(paths[EXPORT_URL]) == {"get"}
    assert paths[LIST_URL]["get"]["security"] == [{"BearerAuth": []}]


# --- Generating the Excel file from the list ----------------------------------------------------


def test_an_excel_file_queued_from_the_list(db: Session, month: dict[str, int], client: TestClient) -> None:
    run_cutoff(period=MONTH, session=db)  # closed, its Excel file not generated
    assert item(client, MONTH)["export"]["status"] == "NOT_STARTED"

    response = client.post(JOBS_URL, json={"type": "EXPORT", "year": MONTH.start.year, "month": MONTH.start.month})

    assert (response.status_code, response.json()["data"]["status"]) == (202, "QUEUED")
    export = item(client, MONTH)["export"]
    assert (export["status"], export["can_generate"], export["automatic_retry"]) == ("QUEUED", False, False)
    assert due_for_automatic_attempt(db) == [], "queued by a user: not retried automatically meanwhile"
    assert queue(client, "EXPORT", MONTH)["code"] == "JOB_IN_PROGRESS"

    run_queued(session=db)

    export = item(client, MONTH)["export"]
    assert (export["status"], export["last_attempt"]["requested_by"]["name"]) == ("SUCCEEDED", "Finance Admin")
    assert client.get(EXPORT_URL, params=month_params()).status_code == 200


def test_automatic_attempts_stop_at_the_maximum_then_a_user_queues_it(
    db: Session, month: dict[str, int], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "export_max_automatic_attempts", 2)
    run_cutoff(period=MONTH, session=db)
    monkeypatch.setattr(monthly_cutoff_export, "build_workbook", broken)

    assert [result.error is not None for result in retry_exports(session=db)] == [True]  # attempt 1
    assert due_for_automatic_attempt(db) == [MONTH], "1 failure < 2: retried"
    retry_exports(session=db)  # attempt 2
    assert due_for_automatic_attempt(db) == [], "2 failures: the automatic retries stop"

    export = item(client, MONTH)["export"]
    assert {key: export[key] for key in ("status", "reason", "failed_attempts", "automatic_retry", "can_generate")} == {
        "status": "FAILED",
        "reason": "OSError: disk full",
        "failed_attempts": 2,
        "automatic_retry": False,
        "can_generate": True,
    }

    # A user may still queue it; still broken, the job fails and says why.
    queue(client, "EXPORT", MONTH)
    (result,) = run_queued(session=db)
    assert (result.status, result.reason) == ("FAILED", "OSError: disk full")
    assert item(client, MONTH)["export"]["failed_attempts"] == 3

    monkeypatch.setattr(monthly_cutoff_export, "build_workbook", WORKING)  # repaired
    queue(client, "EXPORT", MONTH)
    run_queued(session=db)
    export = item(client, MONTH)["export"]
    assert (export["status"], export["failed_attempts"], export["reason"]) == ("SUCCEEDED", 0, None)


def test_a_failed_regeneration_keeps_the_earlier_file(
    db: Session, month: dict[str, int], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_cutoff(period=MONTH, session=db)
    generate_export(MONTH, session=db)
    ready = item(client, MONTH)["export"]
    monkeypatch.setattr(monthly_cutoff_export, "build_workbook", broken)

    queue(client, "EXPORT", MONTH)
    run_queued(session=db)

    export = item(client, MONTH)["export"]
    assert (export["status"], export["failed_attempts"]) == ("FAILED", 1)
    assert export["file"] == ready["file"], "the last good workbook is still there"
    assert client.get(EXPORT_URL, params=month_params()).status_code == 200


# --- Interrupted work ---------------------------------------------------------------------------


def test_an_interrupted_job_can_be_queued_again(db: Session, month: dict[str, int], client: TestClient) -> None:
    started = now_utc() - timedelta(minutes=settings.cutoff_job_timeout_minutes + 1)
    db.execute(
        insert(MonthlyCutoffJob).values(
            job_type="CUTOFF", period_start=MONTH.start, status="RUNNING", requested_at=started, started_at=started
        )
    )
    db.commit()

    row = item(client, MONTH)["cutoff"]
    assert (row["status"], row["can_generate"], row["last_job"]["reason_code"]) == ("FAILED", True, "INTERRUPTED")
    assert row["reason"] == (
        f"The job was interrupted: it had not finished {settings.cutoff_job_timeout_minutes} minutes after it started."
    )

    assert queue(client, "CUTOFF", MONTH)["data"]["status"] == "QUEUED"
    first, second = jobs(db)
    assert (first.status, first.reason_code, second.status) == ("FAILED", "INTERRUPTED", "QUEUED")


def test_a_running_excel_generation_blocks_another_until_it_is_interrupted(
    db: Session, month: dict[str, int], client: TestClient
) -> None:
    run_cutoff(period=MONTH, session=db)
    cutoff_id = db.scalar(select(MonthlyCutoff.id).where(MonthlyCutoff.is_latest))
    db.execute(
        insert(MonthlyCutoffExport).values(
            cutoff_id=cutoff_id, status="GENERATING", failed_attempts=0, attempt_started_at=now_utc(), warnings=[]
        )
    )
    db.commit()

    assert (item(client, MONTH)["export"]["status"], item(client, MONTH)["export"]["can_generate"]) == (
        "GENERATING",
        False,
    )
    response = client.post(JOBS_URL, json={"type": "EXPORT", "year": MONTH.start.year, "month": MONTH.start.month})
    assert (response.status_code, response.json()["code"]) == (409, "JOB_IN_PROGRESS")

    timeout = timedelta(minutes=settings.export_generation_timeout_minutes + 1)
    db.execute(update(MonthlyCutoffExport).values(attempt_started_at=now_utc() - timeout))
    db.commit()
    export = item(client, MONTH)["export"]
    assert (export["status"], export["failed_attempts"], export["can_generate"]) == ("FAILED", 1, True)
    assert queue(client, "EXPORT", MONTH)["data"]["status"] == "QUEUED"
    run_queued(session=db)
    assert item(client, MONTH)["export"]["status"] == "SUCCEEDED"


def test_an_interrupted_excel_generation_counts_towards_the_maximum(db: Session, month: dict[str, int]) -> None:
    """A process that dies every time (e.g. out of memory) must not be retried forever."""
    run_cutoff(period=MONTH, session=db)
    cutoff_id = db.scalar(select(MonthlyCutoff.id).where(MonthlyCutoff.is_latest))
    started = now_utc() - timedelta(minutes=settings.export_generation_timeout_minutes + 1)
    db.execute(
        insert(MonthlyCutoffExport).values(
            cutoff_id=cutoff_id,
            status="GENERATING",
            failed_attempts=settings.export_max_automatic_attempts - 1,
            attempt_started_at=started,
            warnings=[],
        )
    )
    db.commit()

    assert due_for_automatic_attempt(db) == []


# --- The hourly cron and the worker -------------------------------------------------------------


def test_the_cron_takes_over_a_cut_off_queued_for_the_month(
    db: Session, month: dict[str, int], client: TestClient
) -> None:
    queued = queue(client, "CUTOFF", MONTH)["data"]  # MONTH is the last ended month: the cron's

    assert monthly_cutoff.main(["--pending"]) == 0

    (job,) = jobs(db)
    assert (job.id, job.status, job.requested_by) == (queued["id"], "SUCCEEDED", queued["requested_by"]["id"])
    assert item(client, MONTH)["cutoff"]["status"] == "CREATED"


def test_the_cron_runs_jobs_no_worker_took(db: Session, month: dict[str, int], client: TestClient) -> None:
    run_cutoff(period=MONTH, session=db)
    generate_export(MONTH, session=db)
    queue(client, "CUTOFF", OLDER)

    assert monthly_cutoff.main(["--pending"]) == 0

    assert item(client, OLDER)["cutoff"]["status"] == "CREATED"


def test_the_cron_keeps_only_its_latest_waiting_attempt(
    db: Session, month: dict[str, int], client: TestClient, operational_month_end: object
) -> None:
    operational_month_end.missing = {"fish-weight-estimate"}

    for _ in range(3):  # three hours
        assert monthly_cutoff.main(["--pending"]) == 3

    (job,) = jobs(db)
    assert (job.status, job.reason_code, job.requested_by) == ("FAILED", "OPERATIONAL_DATA_NOT_READY", None)
    assert item(client, MONTH)["cutoff"]["status"] == "WAITING_FOR_DATA"


def test_the_worker(db: Session, month: dict[str, int], client: TestClient) -> None:
    queue(client, "CUTOFF", OLDER)
    queue(client, "CUTOFF", MONTH)

    assert monthly_cutoff_worker.main(["--once"]) == 0

    assert [job.status for job in jobs(db)] == ["SUCCEEDED", "SUCCEEDED"]
    assert monthly_cutoff_worker.main(["--once"]) == 0, "nothing queued"
