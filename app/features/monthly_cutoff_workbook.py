"""The Excel workbook of a monthly cut-off, for the finance team (built by
``app.features.monthly_cutoff_export``; this module only lays it out).

Its input is what the API returns: the ``GET /monthly-cutoffs`` payload of the cut-off and the
month's ``cash_statements`` rows. So the workbook always shows the same figures as the API, in a
form meant for people:

- amounts are numbers (IDR, no decimals) and dates are dates, so they can be summed and filtered;
- codes are replaced by labels (``FEED_LELE`` -> "Catfish feed");
- every table has a header and a totals row; long tables keep their headings visible when
  scrolling; every sheet prints one page wide (the narrow ones in portrait).

Sheets: Summary, Profit and Loss, Cash Statement, Categories, Loans, Receivables,
Potential Revenue.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.core.config import settings

# --- Formats and styles -------------------------------------------------------------------------

IDR = "#,##0;[Red]-#,##0"
KG = "#,##0.000"
PRICE = "#,##0.00"
COUNT = "#,##0"
DAY = "dd mmm yyyy"

GREEN = "1F6F43"
TITLE_FONT = Font(size=16, bold=True, color=GREEN)
SUBTITLE_FONT = Font(italic=True, color="595959")
SECTION_FONT = Font(size=12, bold=True, color=GREEN)
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor=GREEN)
TOTAL_FONT = Font(bold=True)
TOTAL_BORDER = Border(top=Side(style="thin"), bottom=Side(style="double"))
KEY_FONT = Font(bold=True, size=12)
NOTE_FONT = Font(italic=True, color="7F7F7F")
WARNING_FONT = Font(color="9C5700")
WARNING_FILL = PatternFill("solid", fgColor="FFF2CC")
WRAP = Alignment(wrap_text=True, vertical="top")
LEFT = Alignment(horizontal="left", indent=1)
NUMBER_FORMATS = (IDR, KG, PRICE, COUNT)
NOTE_COLUMN = 4  # on the Summary and Profit and Loss sheets: label, amount, spacer, note

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# --- Labels -------------------------------------------------------------------------------------

CATEGORY_LABELS = {
    "EGG_SALE": "Egg sales",
    "FISH_SALE": "Fish sales",
    "FEED_AYAM": "Chicken feed",
    "FEED_LELE": "Catfish feed",
    "SALARY": "Salaries",
    "UTILITY": "Utilities",
    "HEALTH": "Animal health",
    "LELE_SEED_PURCHASE": "Catfish seed purchases",
    "PULLET_PURCHASE": "Pullet purchases",
    "BANK_ADMIN_FEE": "Bank admin fees",
    "TRANSFER_FEE": "Main account transfer fees",
    "MANUAL_ADJUSTMENT": "Manual adjustments",
    "LOAN_REPAYMENT": "Loan repayments",
    "LOAN_INTEREST": "Loan interest",
    "LOAN_PRINCIPAL": "Loan principal repaid",
    "MANUAL_ADJUSTMENT_IN": "Manual adjustments (money in)",
    "MANUAL_ADJUSTMENT_OUT": "Manual adjustments (money out)",
    "OPENING_BALANCE": "Go-live opening balance",
}
# "OTHER" is both a revenue and an expense type.
OTHER_LABELS = {"revenues": "Other revenue", "REVENUE": "Other revenue"}
OTHER_EXPENSE_LABEL = "Other expenses"
SOURCE_LABELS = {
    "revenues": "Sales and other revenue",
    "operational_cashflows": "Operational expenses",
    "main_cash_account_logs": "Main account adjustments",
    "loans": "Loans",
}
DIRECTION_LABELS = {"credit": "Money in", "debit": "Money out"}
LOAN_TYPE_LABELS = {"SHORT_TERM": "Short term", "LONG_TERM": "Long term"}
EGG_LABELS = {"UTUH": ("Utuh", "Whole eggs"), "PECAH": ("Pecah", "Cracked eggs"), "REJECT": ("Reject", "Rejected eggs")}
SECTION_TITLES = (
    ("REVENUE", "Revenue", "Total revenue"),
    ("OPERATING_EXPENSE", "Operating expenses", "Total operating expenses"),
    ("OTHER_EXPENSE", "Other expenses", "Total other expenses"),
    ("FINANCE_COST", "Finance costs", "Total finance costs"),
)


def humanize(code: str) -> str:
    return code.replace("_", " ").capitalize()


def category_label(code: str, context: str | None = None) -> str:
    """``context``: the statement source table or the P&L section the code is listed under."""
    if code == "OTHER":
        return OTHER_LABELS.get(context or "", OTHER_EXPENSE_LABEL)
    return CATEGORY_LABELS.get(code, humanize(code))


def month_label(year: int, month: int) -> str:
    return f"{MONTHS[month - 1]} {year}"


def long_date(value: date) -> str:
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def local_time(value: str | datetime | None) -> str:
    if value is None:
        return "-"
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return f"{moment.astimezone(settings.timezone):%d %b %Y %H:%M}"


# --- Input --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementLine:
    """A row of the Cash Statement sheet: a ``cash_statements`` row of the month, or a go-live
    opening balance that became effective during it (cash that is not on the statement)."""

    day: date
    description: str
    category: str  # a statement category code, or OPENING_BALANCE
    source_table: str | None
    counterparty: str | None
    reference: str | None
    transaction_date: date | None
    money_in: Decimal | None
    money_out: Decimal | None
    balance: Decimal


@dataclass(frozen=True)
class WorkbookData:
    cutoff: dict[str, Any]  # the GET /monthly-cutoffs payload ("data")
    statement: list[StatementLine]  # oldest first, balance running from the opening cash
    generated_at: datetime
    warnings: list[str]  # found while exporting (e.g. the statement changed after the close)


def idr(value: str | int | Decimal | None) -> int | None:
    """API amounts are whole rupiah strings."""
    return None if value is None else int(Decimal(value))


def decimal(value: str | Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def day_of(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


# --- Writing ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    header: str
    width: float
    number_format: str | None = None
    indent: int = 0  # for text right after a number column, so the two do not run together


class Sheet:
    """Writes a sheet top to bottom."""

    def __init__(self, workbook: Workbook, title: str, columns: Sequence[float], *, portrait: bool = False) -> None:
        self.ws: Worksheet = workbook.create_sheet(title)
        self.row = 1
        for index, width in enumerate(columns, start=1):
            self.ws.column_dimensions[get_column_letter(index)].width = width
        self.ws.sheet_view.showGridLines = False
        setup = self.ws.page_setup
        setup.orientation = "portrait" if portrait else "landscape"
        setup.paperSize = self.ws.PAPERSIZE_A4
        setup.fitToWidth, setup.fitToHeight = 1, 0
        self.ws.sheet_properties.pageSetUpPr.fitToPage = True

    def cell(self, column: int, value: Any, number_format: str | None = None, **style: Any) -> None:
        cell = self.ws.cell(row=self.row, column=column, value=value)
        if number_format and value is not None and not isinstance(value, str):
            cell.number_format = number_format
            if number_format == DAY:
                cell.alignment = LEFT  # like the text around them (Excel right-aligns dates)
        for name, item in style.items():
            setattr(cell, name, item)

    def skip(self, rows: int = 1) -> None:
        self.row += rows

    def title(self, text: str, *subtitles: str) -> None:
        self.cell(1, text, font=TITLE_FONT)
        self.skip()
        for subtitle in subtitles:
            self.cell(1, subtitle, font=SUBTITLE_FONT)
            self.skip()
        self.skip()

    def section(self, text: str) -> None:
        self.cell(1, text, font=SECTION_FONT)
        self.skip()

    def note(self, text: str) -> None:
        self.cell(1, text, font=NOTE_FONT)
        self.skip()

    def warnings(self, items: Sequence[str], span: int) -> None:
        for item in items:
            self.cell(1, item, font=WARNING_FONT, fill=WARNING_FILL, alignment=WRAP)
            self.ws.merge_cells(start_row=self.row, start_column=1, end_row=self.row, end_column=span)
            self.ws.row_dimensions[self.row].height = 15 * (1 + len(item) // 110)
            self.skip()

    def value_line(self, label: str, value: Any, number_format: str = IDR, *, total: bool = False, note: str | None = None) -> None:
        font = TOTAL_FONT if total else None
        border = TOTAL_BORDER if total else Border()
        self.cell(1, label, font=font or Font())
        self.cell(2, value, number_format, font=font or Font(), border=border)
        if note:
            self.cell(NOTE_COLUMN, note, font=NOTE_FONT)
        self.skip()

    def table(
        self,
        columns: Sequence[Column],
        rows: Iterable[Sequence[Any]],
        totals: Sequence[Any] | None = None,
        *,
        filters: bool = False,
        freeze: bool = False,
        empty: str = "None.",
    ) -> None:
        header_row = self.row
        for index, column in enumerate(columns, start=1):
            # Over numbers (right-aligned) the heading is right-aligned too, or it reads as the next column's.
            side = "right" if column.number_format in NUMBER_FORMATS else "left"
            indent = 1 if column.number_format == DAY else column.indent  # dates are indented (see cell)
            alignment = Alignment(horizontal=side, vertical="center", wrap_text=True, indent=indent)
            self.cell(index, column.header, font=HEADER_FONT, fill=HEADER_FILL, alignment=alignment)
        self.ws.row_dimensions[header_row].height = 30
        self.skip()
        first = self.row
        for values in rows:
            for index, (column, value) in enumerate(zip(columns, values, strict=True), start=1):
                self.cell(index, value, column.number_format)
                if column.indent:
                    self.ws.cell(row=self.row, column=index).alignment = Alignment(indent=column.indent)
            self.skip()
        if self.row == first:
            self.cell(1, empty, font=NOTE_FONT)
            self.skip()
        elif filters:
            self.ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(columns))}{self.row - 1}"
        if freeze and self.ws.freeze_panes is None:
            self.ws.freeze_panes = f"A{header_row + 1}"
            self.ws.print_title_rows = f"{header_row}:{header_row}"
        if totals is not None and self.row > first:
            for index, (column, value) in enumerate(zip(columns, totals, strict=True), start=1):
                self.cell(index, value, column.number_format, font=TOTAL_FONT, border=TOTAL_BORDER)
            self.skip()
        self.skip()


def total(values: Iterable[int | Decimal | None]) -> Any:
    return sum((value for value in values if value is not None), 0)


# --- Sheets -------------------------------------------------------------------------------------


class Report:
    def __init__(self, data: WorkbookData) -> None:
        self.data = data
        self.cutoff = data.cutoff
        period = self.cutoff["period"]
        self.month = month_label(period["year"], period["month"])
        self.start, self.end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
        self.workbook = Workbook()
        self.workbook.remove(self.workbook.active)  # type: ignore[arg-type]

    def build(self) -> bytes:
        self.summary()
        self.profit_and_loss()
        self.cash_statement()
        self.categories()
        self.loans()
        self.receivables()
        self.potential_revenue()
        properties = self.workbook.properties
        properties.title = f"Monthly cut-off {self.month}"
        properties.subject = f"Monthly cut-off {self.cutoff['id']}"
        properties.creator = settings.app_name
        properties.created = self.data.generated_at.astimezone(UTC).replace(tzinfo=None)  # openpyxl: naive UTC
        buffer = BytesIO()
        self.workbook.save(buffer)
        return buffer.getvalue()

    def at_end(self, what: str) -> str:
        return f"{what} — at {long_date(self.end)}"

    # Summary ------------------------------------------------------------------------------------

    def summary(self) -> None:
        cutoff = self.cutoff
        sheet = Sheet(self.workbook, "Summary", (46, 20, 2, 70), portrait=True)
        sheet.title(
            f"Monthly Cut-off — {self.month}",
            f"{long_date(self.start)} to {long_date(self.end)}. All amounts in Indonesian rupiah (IDR).",
            f"Monthly cut-off #{cutoff['id']} (revision {cutoff['revision']}), closed "
            f"{local_time(cutoff['generated_at'])}; workbook generated {local_time(self.data.generated_at)} "
            f"({settings.app_timezone}).",
        )
        warnings = [*self.data.warnings, *cutoff["warnings"]]
        if warnings:
            sheet.section("Please check")
            sheet.warnings(warnings, span=NOTE_COLUMN)
            sheet.skip()

        cash = cutoff["cash"]
        sheet.section("Cash")
        sheet.value_line("Opening cash", idr(cash["opening"]), note="Cash at the end of the previous month.")
        if idr(cash["opening_balances_in_period"]):
            sheet.value_line("Go-live opening balances", idr(cash["opening_balances_in_period"]))
        sheet.value_line("Money in", idr(cash["cash_in"]))
        sheet.value_line("Money out", idr(cash["cash_out"]))
        sheet.value_line("Closing cash", idr(cash["closing"]), total=True, note="See the Cash Statement sheet.")
        sheet.skip()

        pl = cutoff["profit_and_loss"]
        sheet.section("Profit and loss (cash basis)")
        sheet.value_line("Revenue", idr(pl["revenue"]))
        sheet.value_line("Operating expenses", idr(pl["operating_expenses"]))
        sheet.value_line("Other expenses", idr(pl["other_expenses"]))
        sheet.value_line("Finance costs", idr(pl["finance_costs"]))
        sheet.value_line("Net profit", idr(pl["net_profit"]), total=True, note="See the Profit and Loss sheet.")
        sheet.skip()

        loans = cutoff["loans"]
        sheet.section(f"Loans at {long_date(self.end)}")
        sheet.value_line("Loans still owed", loans["outstanding_count"], COUNT)
        sheet.value_line("Principal outstanding", idr(loans["principal_outstanding"]))
        sheet.value_line("Interest outstanding", idr(loans["interest_outstanding"]))
        sheet.value_line("Overdue", idr(loans["overdue_amount"]), note="Installments due by the month's end, not paid.")
        sheet.value_line("Principal repaid this month", idr(loans["principal_repaid"]))
        sheet.value_line("Interest paid this month", idr(loans["interest_repaid"]))
        sheet.skip()

        receivables = cutoff["receivables"]
        sheet.section(f"Receivables at {long_date(self.end)}")
        sheet.value_line(
            "Owed by customers", idr(receivables["outstanding"]), note=f"{receivables['invoices']} sale(s) not fully paid."
        )
        sheet.value_line(
            "Waiting for transfer verification",
            idr(receivables["pending_verification"]),
            note="Direct transfers not approved yet; not counted as owed.",
        )
        sheet.skip()

        potential = cutoff.get("potential_revenue")
        sheet.section(f"Potential revenue at {long_date(self.end)}")
        if potential is None:
            sheet.note("Not part of this cut-off (closed before potential revenue was reported).")
        else:
            sheet.value_line("Egg stock", idr(potential["eggs"]["value"]))
            sheet.value_line("Fish in ponds", idr(potential["fish"]["value"]))
            sheet.value_line(
                "Total potential revenue",
                idr(potential["total"]),
                total=True,
                note="Month-end stock at sales prices. Not cash, not in the profit and loss.",
            )
        sheet.skip()

        sheet.section("Sheets in this workbook")
        for name, about in (
            ("Profit and Loss", "Revenue and expenses of the month by category, and how they reconcile with cash."),
            ("Cash Statement", "Every movement of money of the month, with the running cash balance."),
            ("Categories", "The cash statement of the month totalled by category."),
            ("Loans", "Every loan owed at the month's end or repaid during the month."),
            ("Receivables", "What each customer still owed at the month's end, by age."),
            ("Potential Revenue", "Egg stock and fish in ponds at the month's end, valued at sales prices."),
        ):
            sheet.cell(1, name, font=TOTAL_FONT)
            sheet.cell(NOTE_COLUMN, about)
            sheet.skip()
        sheet.skip()
        sheet.note(
            "A copy of the monthly cut-off: it does not change when entries are made later. "
            "A month closed again has a new cut-off and a new workbook."
        )

    # Profit and loss ------------------------------------------------------------------------------

    def profit_and_loss(self) -> None:
        pl = self.cutoff["profit_and_loss"]
        lines = [(item["section"], item["line"], idr(item["amount"])) for item in pl["lines"]]
        sheet = Sheet(self.workbook, "Profit and Loss", (46, 20, 2, 60), portrait=True)
        sheet.title(
            f"Profit and Loss — {self.month}",
            "Cash basis: income and expenses are counted when the money moved (the cash statement of the month).",
        )
        totals = {
            "REVENUE": pl["revenue"],
            "OPERATING_EXPENSE": pl["operating_expenses"],
            "OTHER_EXPENSE": pl["other_expenses"],
            "FINANCE_COST": pl["finance_costs"],
        }
        for section, heading, total_label in SECTION_TITLES:
            sheet.section(heading)
            items = [(line, amount) for kind, line, amount in lines if kind == section]
            for line, amount in items:
                sheet.value_line(f"    {category_label(line, section)}", amount)
            if not items:
                sheet.note("    None this month.")
            sheet.value_line(total_label, idr(totals[section]), total=True)
            sheet.skip()
        sheet.value_line("NET PROFIT", idr(pl["net_profit"]), total=True)
        sheet.ws.cell(row=sheet.row - 1, column=1).font = KEY_FONT
        sheet.skip()

        def non_pl(line: str) -> int:
            return sum((amount or 0 for kind, name, amount in lines if (kind, name) == ("NON_PL", line)), 0)

        sheet.section("Cash movements that are not income or expenses")
        others = [(line, amount) for kind, line, amount in lines if kind == "NON_PL"]
        for line, amount in others:
            sheet.value_line(f"    {category_label(line)}", amount)
        if not others:
            sheet.note("    None this month.")
        sheet.skip()

        cash = self.cutoff["cash"]
        net = idr(pl["net_profit"]) or 0
        bridged = net - non_pl("LOAN_PRINCIPAL") + non_pl("MANUAL_ADJUSTMENT_IN") - non_pl("MANUAL_ADJUSTMENT_OUT")
        movement = (idr(cash["cash_in"]) or 0) - (idr(cash["cash_out"]) or 0)
        sheet.section("Reconciliation with the cash statement")
        sheet.value_line("Net profit", net)
        sheet.value_line("less loan principal repaid", -non_pl("LOAN_PRINCIPAL"))
        sheet.value_line("plus manual adjustments (money in)", non_pl("MANUAL_ADJUSTMENT_IN"))
        sheet.value_line("less manual adjustments (money out)", -non_pl("MANUAL_ADJUSTMENT_OUT"))
        sheet.value_line("Net cash movement from the profit and loss", bridged, total=True)
        sheet.value_line("Money in less money out (cash statement)", movement)
        sheet.value_line(
            "Difference",
            bridged - movement,
            total=True,
            note="0 when the profit and loss reconciles with the cash statement.",
        )

    # Cash statement -------------------------------------------------------------------------------

    def cash_statement(self) -> None:
        cash = self.cutoff["cash"]
        statement = self.data.statement
        opening = idr(cash["opening"]) or 0
        sheet = Sheet(self.workbook, "Cash Statement", (6, 13, 48, 26, 24, 16, 16, 17, 13, 26))
        sheet.title(
            f"Cash Statement — {self.month}",
            "Every movement of money of the month, oldest first. Balance: the cash of the business "
            "(opening cash plus money in less money out).",
        )
        if self.data.warnings:
            sheet.warnings(self.data.warnings, span=10)
            sheet.skip()
        closing = statement[-1].balance if statement else Decimal(opening)
        rows: list[list[Any]] = [
            [None, self.start, "Opening cash (end of the previous month)", None, None, None, None, opening, None, None]
        ]
        rows += [
            [
                number,
                line.day,
                line.description,
                category_label(line.category, line.source_table),
                line.counterparty,
                idr(line.money_in),
                idr(line.money_out),
                idr(line.balance),
                line.transaction_date,
                line.reference,
            ]
            for number, line in enumerate(statement, start=1)
        ]
        sheet.table(
            [
                Column("No.", 6, COUNT),
                Column("Date", 13, DAY),
                Column("Description", 48),
                Column("Category", 26),
                Column("Customer / employee", 24),
                Column("Money in", 16, IDR),
                Column("Money out", 16, IDR),
                Column("Balance", 17, IDR),
                Column("Transaction date", 13, DAY),
                Column("Reference", 26, indent=1),
            ],
            rows,
            totals=[
                None,
                self.end,
                "Closing cash",
                None,
                None,
                total(line.money_in for line in statement),
                total(line.money_out for line in statement),
                idr(closing),
                None,
                None,
            ],
            filters=True,
            freeze=True,
        )
        sheet.note(
            "Transaction date: the date of the sale or expense when it differs from the payment date "
            "(e.g. a sale of an earlier month paid this month). Reference: the transaction in the finance application."
        )

    # Categories -----------------------------------------------------------------------------------

    def categories(self) -> None:
        items = self.cutoff["categories"]
        sheet = Sheet(self.workbook, "Categories", (30, 34, 10, 18), portrait=True)
        sheet.title(f"Cash Statement by Category — {self.month}", "The cash statement of the month, totalled.")
        columns = [
            Column("Source", 30),
            Column("Category", 34),
            Column("Entries", 10, COUNT),
            Column("Amount", 18, IDR),
        ]
        for direction, heading in DIRECTION_LABELS.items():
            chosen = [item for item in items if item["direction"] == direction]
            sheet.section(heading)
            sheet.table(
                columns,
                [
                    [
                        SOURCE_LABELS.get(item["source_table"], humanize(item["source_table"])),
                        category_label(item["category"], item["source_table"]),
                        item["rows"],
                        idr(item["amount"]),
                    ]
                    for item in chosen
                ],
                totals=[
                    f"Total {heading.lower()}",
                    None,
                    sum(item["rows"] for item in chosen),
                    total(idr(item["amount"]) for item in chosen),
                ],
                empty="None this month.",
            )

    # Loans ----------------------------------------------------------------------------------------

    def loans(self) -> None:
        loans = self.cutoff["loans"]
        items = loans["items"]
        sheet = Sheet(self.workbook, "Loans", (26, 16, 12, 13, 13, *([16] * 6), 12, 16, 16, 16))
        sheet.title(
            self.at_end("Loans"),
            "Every loan owed at the month's end or repaid during the month, as of the month's last day.",
        )
        money = ("principal_amount", "principal_paid", "principal_outstanding", "interest_total", "interest_paid",
                 "interest_outstanding")
        sheet.table(
            [
                Column("Lender", 26),
                Column("Reference", 16),
                Column("Type", 12),
                Column("Start", 13, DAY),
                Column("Maturity", 13, DAY),
                Column("Principal", 16, IDR),
                Column("Principal paid", 16, IDR),
                Column("Principal outstanding", 16, IDR),
                Column("Interest (total)", 16, IDR),
                Column("Interest paid", 16, IDR),
                Column("Interest outstanding", 16, IDR),
                Column("Overdue installments", 12, COUNT),
                Column("Overdue amount", 16, IDR),
                Column("Principal repaid this month", 16, IDR),
                Column("Interest paid this month", 16, IDR),
            ],
            [
                [
                    item["lender"]["name"],
                    item["reference_number"],
                    LOAN_TYPE_LABELS.get(item["loan_type"], humanize(item["loan_type"])),
                    day_of(item["start_date"]),
                    day_of(item["maturity_date"]),
                    *(idr(item[key]) for key in money),
                    item["overdue_installments"],
                    idr(item["overdue_amount"]),
                    idr(item["principal_repaid_in_period"]),
                    idr(item["interest_repaid_in_period"]),
                ]
                for item in items
            ],
            totals=[
                "Total",
                None,
                None,
                None,
                None,
                *(total(idr(item[key]) for item in items) for key in money),
                sum(item["overdue_installments"] for item in items),
                idr(loans["overdue_amount"]),
                idr(loans["principal_repaid"]),
                idr(loans["interest_repaid"]),
            ],
            freeze=True,
            empty="No loan owed at the month's end or repaid during the month.",
        )

    # Receivables ----------------------------------------------------------------------------------

    def receivables(self) -> None:
        receivables = self.cutoff["receivables"]
        customers = receivables["customers"]
        sheet = Sheet(self.workbook, "Receivables", (30, 10, 16, 14, 14, 14, 14, 18))
        sheet.title(
            self.at_end("Receivables"),
            "What customers still owed for the sales dated up to the month's end, by age of the sale (days).",
            "Waiting for verification: direct transfers not approved yet, not counted as owed.",
        )
        aging = ("days_0_30", "days_31_60", "days_61_90", "over_90_days")
        rows = [
            [
                item["customer"]["name"],
                item["invoices"],
                idr(item["outstanding"]),
                *(idr(item["aging"][key]) for key in aging),
                idr(item["pending_verification"]),
            ]
            for item in customers
        ]
        sheet.table(
            [
                Column("Customer", 30),
                Column("Sales", 10, COUNT),
                Column("Owed", 16, IDR),
                Column("0-30 days", 14, IDR),
                Column("31-60 days", 14, IDR),
                Column("61-90 days", 14, IDR),
                Column("Over 90 days", 14, IDR),
                Column("Waiting for verification", 18, IDR),
            ],
            rows,
            totals=[
                "Total",
                receivables["invoices"],
                idr(receivables["outstanding"]),
                *(total(idr(item["aging"][key]) for item in customers) for key in aging),
                idr(receivables["pending_verification"]),
            ],
            filters=True,
            freeze=True,
            empty="No customer owed anything at the month's end.",
        )

    # Potential revenue ----------------------------------------------------------------------------

    def potential_revenue(self) -> None:
        potential = self.cutoff.get("potential_revenue")
        sheet = Sheet(self.workbook, "Potential Revenue", (22, 24, 14, 14, 16, 18, 10, 18))
        sheet.title(
            self.at_end("Potential Revenue"),
            "Stock at the month's end valued at the average sales price per kg of the month (or of the latest "
            "earlier month with sales).",
            "Not cash and not in the profit and loss: it becomes revenue when it is sold.",
        )
        if potential is None:
            sheet.note("Not part of this cut-off (closed before potential revenue was reported).")
            return

        def price_cells(item: dict[str, Any]) -> list[Any]:
            price = item["price"]
            if price is None:
                return [None, "No sale to price it", None]
            year, month = (int(part) for part in price["month"].split("-"))
            return [decimal(price["price_per_kg"]), month_label(year, month), price["sales"]]

        def source_note(source: dict[str, Any] | None, what: str) -> None:
            if source:
                sheet.note(
                    f"{what} from operational-service, revision {source['revision']}, "
                    f"finalized {local_time(source['finalized_at'])}."
                )

        eggs = potential["eggs"]
        sheet.section("Egg stock")
        source_note(eggs.get("source"), "Egg stock")
        sheet.table(
            [
                Column("Egg category", 22),
                Column("Description", 24),
                Column("Pieces", 14, COUNT),
                Column("Weight (kg)", 14, KG),
                Column("Price per kg", 16, PRICE),
                Column("Price from sales of", 18, indent=1),
                Column("Sales used", 10, COUNT),
                Column("Value", 18, IDR),
            ],
            [
                [
                    *EGG_LABELS.get(item["egg_category"], (item["egg_category"], None)),
                    item["quantity_pieces"],
                    decimal(item["weight_kg"]),
                    *price_cells(item),
                    idr(item["value"]),
                ]
                for item in eggs["items"]
            ],
            totals=[
                "Total eggs", None, eggs["quantity_pieces"], decimal(eggs["weight_kg"]), None, None, None,
                idr(eggs["value"]),
            ],
            empty="No egg stock reported.",
        )

        fish = potential["fish"]
        sheet.section("Fish in ponds")
        source_note(fish.get("source"), "Fish weight estimate")
        sheet.table(
            [
                Column("Pond code", 22),
                Column("Pond", 24),
                Column("Fish (estimated)", 14, COUNT),
                Column("Weight (kg, estimated)", 14, KG),
                Column("Price per kg", 16, PRICE),
                Column("Price from sales of", 18, indent=1),
                Column("Sales used", 10, COUNT),
                Column("Value", 18, IDR),
            ],
            [
                [
                    item["pond"]["code"],
                    item["pond"]["name"],
                    item["fish_count"],
                    decimal(item["weight_kg"]),
                    *price_cells(item),
                    idr(item["value"]),
                ]
                for item in fish["ponds"]
            ],
            totals=[
                "Total fish", None, fish["fish_count"], decimal(fish["weight_kg"]), None, None, None, idr(fish["value"])
            ],
            empty="No fish reported in the ponds.",
        )
        sheet.cell(1, "Total potential revenue", font=KEY_FONT)
        sheet.cell(8, idr(potential["total"]), IDR, font=KEY_FONT, border=TOTAL_BORDER)


def build_workbook(data: WorkbookData) -> bytes:
    """The ``.xlsx`` file of a cut-off."""
    return Report(data).build()
