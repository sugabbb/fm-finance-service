"""Checks for the Excel export of the monthly cut-off (app/features/monthly_cutoff_export.py,
app/features/monthly_cutoff_workbook.py, app/integrations/export_storage.py and the scripts).

Built on the closed month of test_monthly_cutoff (its ``month`` fixture and figures worked out by
hand): the workbook must show exactly the figures of the API, readable (labels, numbers, dates).
"""

from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

import pytest
import test_monthly_cutoff as scenario
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_monthly_cutoff import MONTH, URL, cutoff_rows, day, month_params

from app.db.models import CashStatement, MonthlyCutoff, MonthlyCutoffAsset, MonthlyCutoffExport, Revenue, User
from app.features import monthly_cutoff_export
from app.features.cash_statement_service import Period
from app.features.finance_service import pay_revenue
from app.features.monthly_cutoff_export import XLSX, CutoffNotFoundError, due_for_automatic_attempt, generate_export
from app.features.monthly_cutoff_service import run_cutoff
from app.features.monthly_cutoff_workbook import month_label
from app.integrations.export_storage import ExportFileMissing, LocalExportStorage
from app.scripts import export_monthly_cutoff, monthly_cutoff

# The closed month of test_monthly_cutoff, and its API client.
month = scenario.month
client = scenario.client

pytestmark = pytest.mark.usefixtures("operational_month_end")

EXPORT_URL = "/api/v1/monthly-cutoffs/export"
SHEETS = [
    "Summary",
    "Profit and Loss",
    "Cash Statement",
    "Categories",
    "Loans",
    "Receivables",
    "Potential Revenue",
]


def workbook(content: bytes) -> dict[str, Worksheet]:
    book = load_workbook(BytesIO(content))
    return {sheet.title: sheet for sheet in book.worksheets}


def rows(sheet: Worksheet) -> list[tuple]:
    """Non-empty rows, without their trailing empty cells."""
    found = []
    for row in sheet.iter_rows(values_only=True):
        values = list(row)
        while values and values[-1] is None:
            values.pop()
        if values:
            found.append(tuple(values))
    return found


def lookup(sheet: Worksheet, label: str, column: int = 2) -> object:
    """The value next to a label in column A (the first one)."""
    for row in sheet.iter_rows():
        if row[0].value == label:
            return row[column - 1].value
    raise AssertionError(f"{label!r} not in {sheet.title}")


def stored(export_dir: Path, key: str) -> bytes:
    return LocalExportStorage(export_dir).load(key)


# --- The workbook -------------------------------------------------------------------------------


def test_the_workbook_of_a_closed_month(db: Session, month: dict[str, int], export_storage_dir: Path) -> None:
    closed = run_cutoff(period=MONTH, session=db)

    outcome = generate_export(MONTH, session=db)

    assert (outcome.cutoff_id, outcome.warnings) == (closed.cutoff_id, [])
    assert outcome.storage_key == f"monthly-cutoffs/{MONTH.label}/cutoff-{closed.cutoff_id}.xlsx"
    assert outcome.file_name == f"monthly-cutoff-{MONTH.label}.xlsx"
    content = stored(export_storage_dir, outcome.storage_key)
    record = db.scalar(select(MonthlyCutoffExport))
    assert (record.cutoff_id, record.storage_backend, record.storage_key, record.size_bytes, record.warnings) == (
        closed.cutoff_id,
        "local",
        outcome.storage_key,
        len(content),
        [],
    )
    assert (record.status, record.failed_attempts, record.last_error, record.attempt_requested_by) == (
        "SUCCEEDED",
        0,
        None,
        None,
    )
    sheets = workbook(content)
    assert list(sheets) == SHEETS

    summary = sheets["Summary"]
    assert summary["A1"].value.startswith("Monthly Cut-off — ")
    for label, value in {
        "Opening cash": 900000,
        "Go-live opening balances": 200000,
        "Money in": 5175000,
        "Money out": 859000,
        "Closing cash": 5416000,
        "Revenue": 175000,
        "Operating expenses": 350000,
        "Net profit": -211500,
        "Principal outstanding": 2530000,
        "Owed by customers": 650000,
        "Waiting for transfer verification": 250000,
        "Egg stock": 500000,
        "Total potential revenue": 500000,
    }.items():
        assert lookup(summary, label) == value, label
    assert "Please check" not in [row[0] for row in rows(summary)], "no warnings"

    pl = rows(sheets["Profit and Loss"])
    assert ("    Egg sales", 175000 - 75000) in pl and ("    Other revenue", 75000) in pl
    assert ("    Utilities", 300000) in pl and ("    Other expenses", 50000) in pl, "OTHER is labelled per section"
    assert ("    Main account transfer fees", 6500) in pl and ("    Loan interest", 30000) in pl
    assert ("NET PROFIT", -211500) in pl
    assert ("    Loan principal repaid", 470000) in pl
    assert ("Difference", 0, None, "0 when the profit and loss reconciles with the cash statement.") in pl


def test_the_cash_statement_sheet_runs_from_the_opening_to_the_closing_cash(
    db: Session, month: dict[str, int], export_storage_dir: Path
) -> None:
    run_cutoff(period=MONTH, session=db)
    content = stored(export_storage_dir, generate_export(MONTH, session=db).storage_key)
    sheet = workbook(content)["Cash Statement"]

    found = rows(sheet)
    start = found.index(next(row for row in found if row[0] == "No."))
    header = found[start]
    table = [row + (None,) * (len(header) - len(row)) for row in found[start + 1 : start + 12]]
    assert header == (
        "No.", "Date", "Description", "Category", "Customer / employee", "Money in", "Money out", "Balance",
        "Transaction date", "Reference",
    )
    opening, *entries, closing = table
    assert opening[7] == 900000
    assert len(entries) == 9, "the 8 statement rows of the month and the go-live balance effective in it"
    go_live = next(row for row in entries if row[3] == "Go-live opening balance")
    assert (go_live[1].date(), go_live[5]) == (day(2), 200000)
    balance = 900000
    for row in entries:  # every balance is the previous one plus money in less money out
        balance += (row[5] or 0) - (row[6] or 0)
        assert row[7] == balance
    assert closing[5:8] == (5175000 + 200000, 859000, 5416000)
    assert [row[0] for row in entries] == list(range(1, 10))
    assert all(entries[i][1] <= entries[i + 1][1] for i in range(len(entries) - 1)), "oldest first"
    assert sheet.freeze_panes is not None and sheet.auto_filter.ref is not None

    # The same rows as the API, the categories as labels.
    api = db.scalars(select(CashStatement).where(CashStatement.statement_date.between(MONTH.start, MONTH.end))).all()
    labels = {"revenues": "Revenue", "operational_cashflows": "Operational cashflow", "loans": "Loan",
              "main_cash_account_logs": "Main account log"}
    assert sorted(row[9] for row in entries if row[9]) == sorted(
        f"{labels[item.source_table]} #{item.source_id}" for item in api
    )
    bu_sri = next(row for row in entries if row[4] == "Warung Bu Sri")
    assert (bu_sri[3], bu_sri[5], bu_sri[6]) == ("Egg sales", 100000, None)
    assert bu_sri[8].date() == day(1), "sold on day 1, paid on day 3"
    repayment = next(row for row in entries if row[3] == "Loan repayments")
    assert repayment[8] is None, "a loan's source date is its start date, not a transaction date"


def test_the_other_sheets(db: Session, month: dict[str, int], export_storage_dir: Path) -> None:
    run_cutoff(period=MONTH, session=db)
    sheets = workbook(stored(export_storage_dir, generate_export(MONTH, session=db).storage_key))

    categories = rows(sheets["Categories"])
    assert ("Sales and other revenue", "Egg sales", 1, 100000) in categories
    assert ("Loans", "Loan repayments", 1, 500000) in categories
    assert ("Total money in", None, 3, 5175000) in categories
    assert ("Total money out", None, 5, 859000) in categories

    loans = rows(sheets["Loans"])
    bri = next(row for row in loans if row[0] == "Bank BRI")
    assert bri[1:3] == ("KUR-7", "Long term")
    assert bri[5:8] == (3000000, 470000, 2530000)
    assert bri[11:] == (1, 530000, 470000, 30000)
    assert next(row for row in loans if row[0] == "Total")[7] == 2530000

    receivables = rows(sheets["Receivables"])
    assert ("Agen Jaya", 1, 250000, 0, 250000, 0, 0, 0) in receivables
    assert ("Total", 4, 650000, 150000, 250000, 0, 250000, 250000) in receivables

    potential = rows(sheets["Potential Revenue"])
    assert ("Utuh", "Whole eggs", 320, 20, 25000, month_label(MONTH.start.year, MONTH.start.month), 2, 500000) in potential
    assert ("Pecah", "Cracked eggs", 0, 0, None, "No sale to price it", None, 0) in potential
    assert ("No fish reported in the ponds.",) in potential
    assert ("Total potential revenue", None, None, None, None, None, None, 500000) in potential


def test_a_statement_changed_after_the_close_is_flagged(
    db: Session, actor: User, month: dict[str, int], export_storage_dir: Path
) -> None:
    run_cutoff(period=MONTH, session=db)
    # A late payment of the month, synced into the statement after the close.
    agen = db.scalar(select(Revenue).where(Revenue.transaction_date == MONTH.end - timedelta(days=45)))
    pay_revenue(db, agen, actor, day(20), "FULL", None)
    db.commit()
    from app.features.cash_statement_sync import run_sync

    run_sync(period=MONTH, session=db)

    outcome = generate_export(MONTH, session=db)

    (warning,) = outcome.warnings
    assert warning.startswith(
        f"The cash statement of {MONTH.label} has changed since this cut-off was taken: now 9 entries, "
        "money in 5,425,000, money out 859,000; at the close 8 entries, money in 5,175,000"
    )
    summary = workbook(stored(export_storage_dir, outcome.storage_key))["Summary"]
    assert warning in [row[0] for row in rows(summary)]
    assert db.scalar(select(MonthlyCutoffExport.warnings)) == [warning]


def test_a_month_without_activity(db: Session, month: dict[str, int], export_storage_dir: Path) -> None:
    """Nothing to list: every table says so, nothing breaks."""
    empty = Period(date(MONTH.start.year - 2, MONTH.start.month, 1))
    run_cutoff(period=empty, session=db)

    sheets = workbook(stored(export_storage_dir, generate_export(empty, session=db).storage_key))

    assert lookup(sheets["Summary"], "Closing cash") == 0
    assert ("No loan owed at the month's end or repaid during the month.",) in rows(sheets["Loans"])
    assert ("No customer owed anything at the month's end.",) in rows(sheets["Receivables"])
    assert ("None this month.",) in rows(sheets["Categories"])
    assert ("    None this month.",) in rows(sheets["Profit and Loss"])


def test_a_cut_off_taken_before_potential_revenue(db: Session, month: dict[str, int], export_storage_dir: Path) -> None:
    run_cutoff(period=MONTH, session=db)
    db.execute(update(MonthlyCutoff).values(potential_revenue=None, month_end_sources=None))
    db.execute(MonthlyCutoffAsset.__table__.delete())
    db.commit()

    sheets = workbook(stored(export_storage_dir, generate_export(MONTH, session=db).storage_key))

    note = ("Not part of this cut-off (closed before potential revenue was reported).",)
    assert note in rows(sheets["Potential Revenue"]) and note in rows(sheets["Summary"])


def test_a_restated_month_gets_a_new_workbook(
    db: Session, actor: User, month: dict[str, int], export_storage_dir: Path
) -> None:
    with pytest.raises(CutoffNotFoundError):
        generate_export(MONTH, session=db)
    run_cutoff(period=MONTH, session=db)
    old = generate_export(MONTH, session=db)
    again = generate_export(MONTH, session=db)  # regenerating replaces the cut-off's workbook
    assert again.storage_key == old.storage_key
    assert db.scalar(select(MonthlyCutoffExport.id).where(MonthlyCutoffExport.cutoff_id == old.cutoff_id))

    run_cutoff(period=MONTH, session=db)  # restated: a new cut-off, its export not started
    assert due_for_automatic_attempt(db) == [MONTH]
    new = generate_export(MONTH, session=db)

    assert new.storage_key != old.storage_key
    assert stored(export_storage_dir, old.storage_key), "the earlier workbook is kept"
    assert due_for_automatic_attempt(db) == []


# --- Storage ------------------------------------------------------------------------------------


def test_local_storage(tmp_path: Path) -> None:
    storage = LocalExportStorage(tmp_path)
    storage.save("a/b/file.xlsx", b"one")
    storage.save("a/b/file.xlsx", b"two")  # replaced

    assert storage.load("a/b/file.xlsx") == b"two"
    assert [path.name for path in (tmp_path / "a" / "b").iterdir()] == ["file.xlsx"], "no temporary file left"
    with pytest.raises(ExportFileMissing):
        storage.load("a/b/other.xlsx")
    for key in ("../escape.xlsx", "/etc/passwd", "a//b.xlsx", "a/./b.xlsx", ""):
        with pytest.raises(ValueError):
            storage.save(key, b"x")


# --- API ----------------------------------------------------------------------------------------


def test_api_download(db: Session, month: dict[str, int], client: TestClient, export_storage_dir: Path) -> None:
    assert client.get(EXPORT_URL, params=month_params()).json()["code"] == "NOT_FOUND", "not closed"
    run_cutoff(period=MONTH, session=db)
    response = client.get(EXPORT_URL, params=month_params())
    assert (response.status_code, response.json()["code"]) == (404, "EXPORT_NOT_AVAILABLE")
    export = client.get(URL, params=month_params()).json()["data"]["export"]
    assert (export["status"], export["reason"], export["file"]) == (
        "NOT_STARTED",
        "The Excel file has not been generated yet.",
        None,
    )
    assert response.json()["export"] == export, "the 404 says where the export stands"

    outcome = generate_export(MONTH, session=db)
    response = client.get(EXPORT_URL, params=month_params())

    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX
    assert response.headers["content-disposition"] == f'attachment; filename="monthly-cutoff-{MONTH.label}.xlsx"'
    assert response.content == stored(export_storage_dir, outcome.storage_key)
    assert list(workbook(response.content)) == SHEETS
    export = client.get(URL, params=month_params()).json()["data"]["export"]
    assert export["status"] == "SUCCEEDED"
    file = export["file"]
    assert file["file_name"] == f"monthly-cutoff-{MONTH.label}.xlsx"
    assert file["size_bytes"] == len(response.content) and file["warnings"] == []
    assert file["download_path"] == f"{EXPORT_URL}?year={MONTH.start.year}&month={MONTH.start.month}"

    (export_storage_dir / outcome.storage_key).unlink()  # the file is lost
    assert client.get(EXPORT_URL, params=month_params()).json()["code"] == "EXPORT_NOT_AVAILABLE"


def test_api_download_access_and_validation(db: Session, month: dict[str, int], client: TestClient) -> None:
    run_cutoff(period=MONTH, session=db)
    generate_export(MONTH, session=db)

    assert client.get(EXPORT_URL, params=month_params(), headers={"Authorization": ""}).status_code == 401
    assert client.get(f"{EXPORT_URL}?year=2026").json()["errors"] == {"month": ["Field required"]}
    assert client.get(f"{EXPORT_URL}?year=2026&month=7&month=8").status_code == 422
    operation = client.get("/openapi.json").json()["paths"][EXPORT_URL]["get"]
    assert operation["security"] == [{"BearerAuth": []}]
    assert XLSX in operation["responses"]["200"]["content"]


# --- Commands -----------------------------------------------------------------------------------


def test_closing_a_month_exports_it(db: Session, month: dict[str, int], export_storage_dir: Path) -> None:
    assert monthly_cutoff.main(["--month", MONTH.label, "--dry-run"]) == 0
    assert db.scalar(select(MonthlyCutoffExport)) is None, "a dry run exports nothing"

    assert monthly_cutoff.main(["--month", MONTH.label]) == 0

    (cutoff,) = cutoff_rows(db)
    record = db.scalar(select(MonthlyCutoffExport))
    assert record.cutoff_id == cutoff.id
    assert list(workbook(stored(export_storage_dir, record.storage_key))) == SHEETS


def test_a_failed_export_keeps_the_close_and_pending_retries_it(
    db: Session, month: dict[str, int], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object, **kwargs: object) -> bytes:
        raise OSError("disk full")

    working = monthly_cutoff_export.build_workbook
    monkeypatch.setattr(monthly_cutoff_export, "build_workbook", broken)
    assert monthly_cutoff.main(["--month", MONTH.label]) == 1
    assert "the Excel export failed (the close itself is saved): OSError: disk full" in caplog.text
    assert [row.status for row in cutoff_rows(db)] == ["SUCCESS"]
    record = db.scalar(select(MonthlyCutoffExport))
    assert (record.status, record.failed_attempts, record.last_error, record.storage_key) == (
        "FAILED",
        1,
        "OSError: disk full",
        None,
    )

    monkeypatch.setattr(monthly_cutoff_export, "build_workbook", working)  # fixed: the hourly run catches up
    assert monthly_cutoff.main(["--pending", "--month", MONTH.label]) == 0
    db.refresh(record)
    assert (record.status, record.failed_attempts, record.last_error) == ("SUCCEEDED", 0, None)
    assert record.cutoff_id == cutoff_rows(db)[0].id
    assert len(cutoff_rows(db)) == 1


def test_pending_generates_a_missing_export(
    db: Session, month: dict[str, int], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="app.scripts.monthly_cutoff")
    run_cutoff(period=MONTH, session=db)  # closed, not exported (e.g. the export failed)

    assert monthly_cutoff.main(["--pending", "--month", MONTH.label]) == 0

    assert f"monthly cut-off {MONTH.label}: Excel export generated (automatic retry)" in caplog.text
    assert db.scalar(select(MonthlyCutoffExport.status)) == "SUCCEEDED"
    assert len(cutoff_rows(db)) == 1, "not closed again"
    caplog.clear()
    assert monthly_cutoff.main(["--pending", "--month", MONTH.label]) == 0
    assert "nothing to close" in caplog.text and "automatic retry" not in caplog.text


def test_export_command(db: Session, month: dict[str, int], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="app.scripts.export_monthly_cutoff")
    assert export_monthly_cutoff.main(["--month", MONTH.label]) == 2, "not closed"

    run_cutoff(period=MONTH, session=db)
    assert export_monthly_cutoff.main(["--month", MONTH.label]) == 0

    record = db.scalar(select(MonthlyCutoffExport))
    assert f"saved as monthly-cutoff-{MONTH.label}.xlsx ({record.size_bytes} bytes" in caplog.text
    with pytest.raises(SystemExit):
        export_monthly_cutoff.main([])
