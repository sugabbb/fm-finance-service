"""Checks for ``cash_statements``, the bank-statement style record of every real cash movement.

Rules under test (see app/features/cash_statement_service.py):

- operational cashflows (except MAIN_ACCOUNT_TOPUP): a debit per amount actually paid, once
  PARTIALLY_PAID/PAID, dated on the payment date; several payments = several rows with numbered
  descriptions and the same reference;
- revenues (except MAIN_ACCOUNT_TRANSFER): the same, as credits; an approved direct transfer is one
  row on its transfer date;
- MAIN_ACCOUNT_TRANSFER: only its fee, as a debit, once approved;
- main cash account: only the manual adjustments, never the automatic top-up/transfer entries;
- the table is **not** kept in sync by the write itself any more: a write only makes the row
  eligible, and ``rebuild_cash_statements`` (the one-off ``app.scripts.backfill_cash_statements``)
  is what derives/inserts/updates/deletes the statement rows, idempotently, for the whole history.
  The nightly, month-scoped job has its own tests in tests/test_cash_statement_sync.py.

Most tests below therefore call :func:`sync` (a thin wrapper around ``rebuild_cash_statements``)
after the writes they are checking, standing in for the nightly job; a few explicitly assert that
nothing appears *before* that call, to lock in that the write path no longer syncs live.

Like the other finance tests these run against the real PostgreSQL test database (see
tests/conftest.py), which is wiped before and after every test.
"""

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    AuditEvent,
    CashStatement,
    Customer,
    FinancialOpeningBalance,
    OperationalCashflow,
    OperationalCashflowPayment,
    OperationalCashflowSalaryDetail,
    Revenue,
    RevenuePayment,
    RevenueTransferDetail,
    User,
)
from app.features.cash_balance_service import CASH_ON_HAND, get_balance
from app.features.cash_statement_service import plan_sync, rebuild_cash_statements, source_totals
from app.features.common import today_local
from app.features.finance_service import (
    decide_operational,
    decide_revenue,
    pay_operational,
    pay_revenue,
    save_operational,
    save_revenue,
    submit_operational,
    submit_revenue,
    void_operational_payment,
    void_revenue_payment,
)
from app.features.main_cash_account_events import drain
from app.features.main_cash_account_service import adjust
from app.main import app
from app.scripts.backfill_cash_statements import main

TODAY = today_local()
YESTERDAY = TODAY - timedelta(days=1)
TWO_DAYS_AGO = TODAY - timedelta(days=2)

# --- Helpers ------------------------------------------------------------------------------------


def sync(db: Session) -> None:
    """Brings the statement up to date: writes no longer sync ``cash_statements`` live (see
    app/features/cash_statement_service.py), so tests call this after the writes they want
    reflected on the statement. The full rebuild, so the month of the test data never matters."""
    rebuild_cash_statements(db)


def statements(db: Session) -> list[CashStatement]:
    """Deterministic order: date, reference, sequence, line."""
    db.expire_all()
    return list(
        db.scalars(
            select(CashStatement).order_by(
                CashStatement.statement_date,
                CashStatement.reference,
                CashStatement.payment_sequence,
                CashStatement.source_line,
            )
        ).all()
    )


def lines(db: Session) -> list[tuple[str, str, Decimal, str]]:
    """(date, direction, amount, description)."""
    return [
        (row.statement_date.isoformat(), row.direction, Decimal(row.amount), row.description) for row in statements(db)
    ]


def utility_data(amount: str, description: str = "Electricity") -> dict[str, object]:
    return {
        "cashflow_type": "UTILITY",
        "transaction_date": TWO_DAYS_AGO,
        "amount_idr": Decimal(amount),
        "details": {"description": description},
    }


def topup_data(amount: str) -> dict[str, object]:
    return {
        "cashflow_type": "MAIN_ACCOUNT_TOPUP",
        "transaction_date": TODAY,
        "amount_idr": Decimal(amount),
        "details": {"notes": "Top up"},
    }


def approved_outflow(db: Session, actor: User, data: dict[str, object]) -> OperationalCashflow:
    row = save_operational(db, data, actor)
    submit_operational(db, row, actor)
    decide_operational(db, row, actor, "APPROVED", None)
    db.commit()
    return row


def pay(db: Session, actor: User, row: OperationalCashflow, day: date, amount: str | None = None) -> None:
    mode = "FULL" if amount is None else "PARTIAL"
    pay_operational(db, row, actor, day, mode, None if amount is None else Decimal(amount))
    db.commit()


def egg_sale_data(payment_method: str = "CASH", transfer_date: date | None = None) -> dict[str, object]:
    return {
        "revenue_type": "EGG_SALE",
        "transaction_date": TWO_DAYS_AGO,
        "payment_method": payment_method,
        "direct_transfer_date": transfer_date,
        "customer_id": None,
        "new_customer": {"name": "Warung Bu Sri", "phone": "0812", "customer_type": "WARUNG", "notes": None},
        # 10 kg * 25,000 = 250,000
        "details": [
            {
                "egg_category": "UTUH",
                "weight_kg": Decimal("10"),
                "quantity_pieces": 160,
                "price_per_kg_idr": Decimal("25000"),
            }
        ],
    }


def transfer_data(gross: str, cost: str = "0") -> dict[str, object]:
    return {
        "revenue_type": "MAIN_ACCOUNT_TRANSFER",
        "transaction_date": TODAY,
        "details": {"gross_amount_idr": Decimal(gross), "payment_cost_idr": Decimal(cost), "notes": "Weekly deposit"},
    }


def approved_transfer(db: Session, actor: User, gross: str, cost: str) -> Revenue:
    row = save_revenue(db, transfer_data(gross, cost), actor)
    db.commit()
    submit_revenue(db, row, actor)
    db.commit()
    decide_revenue(db, row, actor, "APPROVED", None, True)
    db.commit()
    return row


# --- Operational cashflows: debit per amount actually paid ---------------------------------------


def test_unpaid_cashflow_is_not_on_the_statement(db: Session, actor: User) -> None:
    approved_outflow(db, actor, utility_data("300000"))
    waiting = save_operational(db, utility_data("100000"), actor)
    submit_operational(db, waiting, actor)
    db.commit()
    sync(db)

    assert statements(db) == []


def test_single_full_payment_is_one_row_dated_on_the_payment(db: Session, actor: User) -> None:
    row = approved_outflow(db, actor, utility_data("300000"))
    pay(db, actor, row, YESTERDAY)
    assert statements(db) == [], "not synced live any more"
    sync(db)

    (statement,) = statements(db)
    assert lines(db) == [(YESTERDAY.isoformat(), "debit", Decimal("300000"), "Utility: Electricity")]
    assert (statement.category, statement.reference) == ("UTILITY", f"operational_cashflows:{row.id}")
    assert (statement.source_table, statement.source_id, statement.source_line) == (
        "operational_cashflows",
        row.id,
        f"payment:{row.payments[0].id}",
    )
    assert (statement.payment_id, statement.payment_sequence) == (row.payments[0].id, 1)
    assert statement.source_transaction_date == TWO_DAYS_AGO, "the transaction date is kept as a detail"
    assert (statement.source_amount, statement.source_payment_status) == (Decimal("300000"), "PAID")


def test_partial_payments_are_one_row_each_with_the_same_reference(db: Session, actor: User) -> None:
    row = approved_outflow(db, actor, utility_data("300000"))

    pay(db, actor, row, TWO_DAYS_AGO, "100000")
    sync(db)
    assert lines(db) == [
        (TWO_DAYS_AGO.isoformat(), "debit", Decimal("100000"), "Utility: Electricity (partial payment 1)")
    ]
    assert statements(db)[0].source_payment_status == "PARTIALLY_PAID"

    pay(db, actor, row, YESTERDAY, "50000")
    pay(db, actor, row, TODAY)
    sync(db)

    assert lines(db) == [
        (TWO_DAYS_AGO.isoformat(), "debit", Decimal("100000"), "Utility: Electricity (partial payment 1)"),
        (YESTERDAY.isoformat(), "debit", Decimal("50000"), "Utility: Electricity (partial payment 2)"),
        (TODAY.isoformat(), "debit", Decimal("150000"), "Utility: Electricity (final payment 3)"),
    ]
    rows = statements(db)
    assert {item.reference for item in rows} == {f"operational_cashflows:{row.id}"}
    assert [item.payment_sequence for item in rows] == [1, 2, 3]
    assert {item.source_payment_status for item in rows} == {"PAID"}, "details follow the transaction"
    assert {item.source_amount for item in rows} == {Decimal("300000")}


def test_voided_payment_leaves_the_statement_and_the_rest_is_renumbered(db: Session, actor: User) -> None:
    row = approved_outflow(db, actor, utility_data("300000"))
    pay(db, actor, row, TWO_DAYS_AGO, "100000")
    pay(db, actor, row, YESTERDAY, "50000")
    sync(db)
    second_id = statements(db)[1].id

    void_operational_payment(db, row, row.payments[0], actor, "Recorded twice")
    db.commit()
    sync(db)

    assert lines(db) == [(YESTERDAY.isoformat(), "debit", Decimal("50000"), "Utility: Electricity (partial payment 1)")]
    assert statements(db)[0].id == second_id, "the remaining row is updated in place"


def test_correcting_a_partially_paid_cashflow_updates_its_rows(db: Session, actor: User) -> None:
    row = approved_outflow(db, actor, utility_data("300000"))
    pay(db, actor, row, TWO_DAYS_AGO, "100000")
    pay(db, actor, row, YESTERDAY, "50000")

    # The amount was wrong: 150,000 is already paid, so the cashflow becomes PAID.
    row = save_operational(db, utility_data("150000"), actor, row, correction=True, reason="Wrong amount")
    db.commit()
    sync(db)

    assert row.payment_status == "PAID"
    assert [item[3] for item in lines(db)] == [
        "Utility: Electricity (partial payment 1)",
        "Utility: Electricity (final payment 2)",
    ]
    assert {item.source_amount for item in statements(db)} == {Decimal("150000")}


def test_topups_are_not_on_the_statement(db: Session, actor: User) -> None:
    save_operational(db, topup_data("1000000"), actor)
    db.commit()
    drain()  # also posts the automatic main cash account entry, which must not appear either
    sync(db)

    assert statements(db) == []


def test_salary_names_the_employee(db: Session, actor: User, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.features.finance_service.operational_client.employee",
        lambda employee_id: {"id": employee_id, "name": "Pak Budi", "status": "ACTIVE"},
    )
    try:
        row = approved_outflow(
            db,
            actor,
            {
                "cashflow_type": "SALARY",
                "transaction_date": TODAY,
                "amount_idr": Decimal("2500000"),
                "details": {"employee_id": 77, "salary_period": "January 2026"},
            },
        )
        pay(db, actor, row, TODAY)
        sync(db)

        (statement,) = statements(db)
        assert (statement.description, statement.counterparty, statement.category) == (
            "Salary: January 2026",
            "Pak Budi",
            "SALARY",
        )
    finally:
        db.rollback()
        db.execute(delete(CashStatement))
        db.execute(delete(OperationalCashflowPayment))
        db.execute(delete(OperationalCashflowSalaryDetail))
        db.execute(delete(AuditEvent).where(AuditEvent.entity_type == "operational_cashflows"))
        db.execute(delete(OperationalCashflow))
        db.commit()


def test_operational_debits_explain_the_cash_on_hand(db: Session, actor: User) -> None:
    """cash_on_hand = top-ups - what was actually paid = top-ups - the statement's cashflow debits."""
    save_operational(db, topup_data("1000000"), actor)
    db.commit()
    partial = approved_outflow(db, actor, utility_data("300000"))
    pay(db, actor, partial, YESTERDAY, "120000")
    paid = approved_outflow(db, actor, utility_data("200000", "Water"))
    pay(db, actor, paid, TODAY)
    approved_outflow(db, actor, utility_data("999999", "Not paid yet"))
    sync(db)

    operational = next(total for total in source_totals(db) if total.source_table == "operational_cashflows")
    assert operational.debit == Decimal("320000")
    assert get_balance(db, CASH_ON_HAND).amount == Decimal("1000000") - operational.debit == Decimal("680000")


# --- Revenues: credit per amount actually received ---------------------------------------------


def test_cash_sale_payments_are_credits_numbered_like_cashflows(db: Session, actor: User) -> None:
    row = save_revenue(db, egg_sale_data(), actor)
    db.commit()
    assert statements(db) == [], "an unpaid sale is not on the statement"

    pay_revenue(db, row, actor, YESTERDAY, "PARTIAL", Decimal("50000"))
    db.commit()
    pay_revenue(db, row, actor, TODAY, "FULL", None)
    db.commit()
    sync(db)

    assert lines(db) == [
        (YESTERDAY.isoformat(), "credit", Decimal("50000"), "Egg sale (partial payment 1)"),
        (TODAY.isoformat(), "credit", Decimal("200000"), "Egg sale (final payment 2)"),
    ]
    rows = statements(db)
    assert {item.reference for item in rows} == {f"revenues:{row.id}"}
    assert {item.counterparty for item in rows} == {"Warung Bu Sri"}
    assert {item.category for item in rows} == {"EGG_SALE"}

    void_revenue_payment(db, row, row.payments[1], actor, "Wrong amount")
    db.commit()
    sync(db)
    assert lines(db) == [(YESTERDAY.isoformat(), "credit", Decimal("50000"), "Egg sale (partial payment 1)")]


def test_direct_transfer_sale_is_credited_on_its_transfer_date_once_approved(db: Session, actor: User) -> None:
    row = save_revenue(db, egg_sale_data("DIRECT_TRANSFER", YESTERDAY), actor)
    db.commit()
    assert statements(db) == [], "submitted, waiting for approval"

    decide_revenue(db, row, actor, "APPROVED", None, False)
    db.commit()
    sync(db)

    assert lines(db) == [(YESTERDAY.isoformat(), "credit", Decimal("250000"), "Egg sale (direct transfer)")]
    (statement,) = statements(db)
    assert (statement.source_line, statement.payment_id, statement.payment_sequence) == ("paid", None, None)


def test_rejected_direct_transfer_is_not_on_the_statement(db: Session, actor: User) -> None:
    row = save_revenue(db, egg_sale_data("DIRECT_TRANSFER", YESTERDAY), actor)
    db.commit()
    decide_revenue(db, row, actor, "REJECTED", "Not received", False)
    db.commit()
    sync(db)

    assert statements(db) == []


# --- Main account transfers: only the fee -------------------------------------------------------


def test_only_the_fee_of_an_approved_transfer_is_on_the_statement(db: Session, actor: User) -> None:
    row = save_revenue(db, transfer_data("5000000", "6500"), actor)
    db.commit()
    submit_revenue(db, row, actor)
    db.commit()
    assert statements(db) == [], "not approved yet"

    decide_revenue(db, row, actor, "APPROVED", None, True)
    db.commit()
    drain()
    sync(db)

    assert lines(db) == [(TODAY.isoformat(), "debit", Decimal("6500"), "Main account transfer fee: Weekly deposit")]
    (fee,) = statements(db)
    assert (fee.category, fee.reference, fee.source_line) == ("TRANSFER_FEE", f"revenues:{row.id}", "fee")
    assert fee.source_amount == Decimal("5000000"), "the transfer's gross amount"


def test_transfer_without_fee_is_not_on_the_statement(db: Session, actor: User) -> None:
    approved_transfer(db, actor, "100000", "0")
    drain()
    sync(db)

    assert statements(db) == []


# --- Main cash account: manual adjustments only -------------------------------------------------


def test_only_manual_adjustments_of_the_main_account_are_on_the_statement(db: Session, actor: User) -> None:
    save_operational(db, topup_data("1000000"), actor)  # automatic debit on the main account
    db.commit()
    approved_transfer(db, actor, "500000", "0")  # automatic credit on the main account
    credit = adjust(db, "credit", Decimal("10000000"), "Opening: bank statement", actor)
    debit = adjust(db, "debit", Decimal("2500"), "Bank admin fee", actor)
    db.commit()
    drain()
    sync(db)

    rows = sorted(statements(db), key=lambda item: item.source_id)
    assert [(item.direction, Decimal(item.amount), item.description) for item in rows] == [
        ("credit", Decimal("10000000"), "Main account adjustment: Opening: bank statement"),
        ("debit", Decimal("2500"), "Main account adjustment: Bank admin fee"),
    ]
    assert [item.reference for item in rows] == [
        f"main_cash_account_logs:{credit.id}",
        f"main_cash_account_logs:{debit.id}",
    ]
    assert {(item.category, item.statement_date) for item in rows} == {("MANUAL_ADJUSTMENT", TODAY)}


def test_writes_are_not_on_the_statement_until_the_nightly_sync_runs(db: Session, actor: User) -> None:
    """The write path only makes rows *eligible*; nothing is written to cash_statements until
    the statement is synced (nightly by app.scripts.sync_cash_statements; here the full rebuild) -
    once it is, everything that happened since is caught up in a single pass."""
    cashflow = approved_outflow(db, actor, utility_data("300000"))
    pay(db, actor, cashflow, YESTERDAY, "100000")
    sale = save_revenue(db, egg_sale_data(), actor)
    db.commit()
    pay_revenue(db, sale, actor, TODAY, "FULL", None)
    db.commit()
    approved_transfer(db, actor, "1000000", "6500")
    adjust(db, "credit", Decimal("1000"), "Interest", actor)
    db.commit()

    assert statements(db) == [], "not synced live"

    sync(db)

    assert len(statements(db)) == 4
    assert plan_sync(db).changed is False, "re-running it changes nothing"


# --- Backfill ------------------------------------------------------------------------------------


@pytest.fixture
def legacy_history(db: Session, actor: User) -> dict[str, int]:
    """Rows written straight into the tables, bypassing the API hooks (pre-existing data)."""
    customer = Customer(name="Agen Jaya", phone="0813", customer_type="AGEN")
    db.add(customer)
    db.flush()

    def cashflow(cashflow_type: str, payment_status: str, value: str, paid_at: datetime | None = None) -> int:
        topup = cashflow_type == "MAIN_ACCOUNT_TOPUP"
        row = OperationalCashflow(
            cashflow_type=cashflow_type,
            direction="INFLOW" if topup else "OUTFLOW",
            transaction_date=date(2026, 1, 10),
            amount_idr=Decimal(value),
            approval_status="NOT_REQUIRED" if topup else "APPROVED",
            payment_status=payment_status,
            paid_at=paid_at,
            created_by=actor.id,
        )
        db.add(row)
        db.flush()
        return row.id

    def payment(cashflow_id: int, value: str, day: date, voided: bool = False) -> int:
        row = OperationalCashflowPayment(
            operational_cashflow_id=cashflow_id,
            paid_date=day,
            amount_idr=Decimal(value),
            payment_mode="PARTIAL",
            created_by=actor.id,
            voided_at=datetime(2026, 2, 1, tzinfo=UTC) if voided else None,
            voided_by=actor.id if voided else None,
            void_reason="duplicate" if voided else None,
        )
        db.add(row)
        db.flush()
        return row.id

    cashflow("MAIN_ACCOUNT_TOPUP", "PAID", "1000000", datetime(2026, 1, 10, tzinfo=UTC))  # excluded
    utility = cashflow("UTILITY", "PARTIALLY_PAID", "300000")
    paid_payment = payment(utility, "100000", date(2026, 1, 12))
    voided_payment = payment(utility, "50000", date(2026, 1, 13), voided=True)
    # PAID without any payment row (recorded outside the API): one row for the full amount.
    feed = cashflow("FEED_AYAM", "PAID", "700000", datetime(2026, 1, 11, tzinfo=UTC))
    cashflow("OTHER", "UNPAID", "999")  # not paid: excluded

    sale = Revenue(
        revenue_type="FISH_SALE",
        direction="INFLOW",
        transaction_date=date(2026, 1, 15),
        amount_idr=Decimal("400000"),
        approval_status="NOT_REQUIRED",
        payment_status="PAID",
        payment_method="CASH",
        customer_id=customer.id,
        created_by=actor.id,
    )
    transfer = Revenue(
        revenue_type="MAIN_ACCOUNT_TRANSFER",
        direction="OUTFLOW",
        transaction_date=date(2026, 1, 20),
        amount_idr=Decimal("4900"),
        approval_status="APPROVED",
        payment_status="PAID",
        paid_at=datetime(2026, 1, 20, tzinfo=UTC),
        created_by=actor.id,
    )
    db.add_all([sale, transfer])
    db.flush()
    db.add(
        RevenuePayment(
            revenue_id=sale.id,
            paid_date=date(2026, 1, 16),
            amount_idr=Decimal("400000"),
            payment_mode="FULL",
            created_by=actor.id,
        )
    )
    db.add(
        RevenueTransferDetail(revenue_id=transfer.id, gross_amount_idr=Decimal("5000"), payment_cost_idr=Decimal("100"))
    )
    # Opening balances are not one of the statement's sources.
    db.add(
        FinancialOpeningBalance(
            account="OPERATIONAL", amount_idr=Decimal("50000"), effective_date=date(2026, 1, 1), created_by=actor.id
        )
    )
    db.commit()
    return {
        "utility": utility,
        "paid_payment": paid_payment,
        "voided_payment": voided_payment,
        "feed": feed,
        "sale": sale.id,
        "transfer": transfer.id,
    }


EXPECTED_LEGACY = [
    ("2026-01-11", "debit", Decimal("700000"), "Feed purchase Ayam"),
    ("2026-01-12", "debit", Decimal("100000"), "Utility (partial payment 1)"),
    ("2026-01-16", "credit", Decimal("400000"), "Fish sale"),
    ("2026-01-20", "debit", Decimal("100"), "Main account transfer fee"),
]


def test_backfill_populates_existing_history(db: Session, legacy_history: dict[str, int]) -> None:
    result = rebuild_cash_statements(db)

    assert (result.plan.inserted, result.plan.updated, result.plan.deleted) == (4, 0, 0)
    assert lines(db) == EXPECTED_LEGACY
    by_reference = {row.reference: row for row in statements(db)}
    assert by_reference[f"operational_cashflows:{legacy_history['feed']}"].source_line == "paid"
    assert (
        by_reference[f"operational_cashflows:{legacy_history['utility']}"].payment_id == legacy_history["paid_payment"]
    )
    assert by_reference[f"revenues:{legacy_history['sale']}"].counterparty == "Agen Jaya"
    totals = {total.source_table: total for total in result.totals}
    assert (totals["operational_cashflows"].rows, totals["operational_cashflows"].debit) == (2, Decimal("800000"))
    assert (totals["revenues"].credit, totals["revenues"].debit) == (Decimal("400000"), Decimal("100"))
    assert totals["main_cash_account_logs"].rows == 0


def test_backfill_is_idempotent_and_dry_run_writes_nothing(db: Session, legacy_history: dict[str, int]) -> None:
    preview = rebuild_cash_statements(db, dry_run=True)
    assert preview.dry_run and preview.plan.inserted == 4
    assert preview.totals[0].rows == 2, "the dry run reports the totals the table would have"
    assert statements(db) == []

    rebuild_cash_statements(db)
    ids = [row.id for row in statements(db)]
    again = rebuild_cash_statements(db)

    assert again.plan.changed is False
    assert [row.id for row in statements(db)] == ids


def test_backfill_repairs_drift(db: Session, actor: User, legacy_history: dict[str, int]) -> None:
    rebuild_cash_statements(db)
    feed_row = next(row.id for row in statements(db) if row.source_id == legacy_history["feed"])

    # Edits made outside the API: an amount change, a late void, an unpaid-again transfer and a
    # stale row that no source justifies any more.
    db.execute(
        update(OperationalCashflow).where(OperationalCashflow.id == legacy_history["feed"]).values(amount_idr=750000)
    )
    db.execute(
        update(OperationalCashflowPayment)
        .where(OperationalCashflowPayment.id == legacy_history["paid_payment"])
        .values(voided_at=datetime(2026, 3, 1, tzinfo=UTC), voided_by=actor.id, void_reason="late void")
    )
    db.execute(update(Revenue).where(Revenue.id == legacy_history["transfer"]).values(payment_status="UNPAID"))
    db.execute(
        insert(CashStatement).values(
            statement_date=date(2026, 1, 1),
            direction="credit",
            amount=1,
            category="OTHER",
            description="stale",
            reference="revenues:999999",
            source_table="revenues",
            source_id=999999,
            source_line="paid",
            source_transaction_date=date(2026, 1, 1),
            source_amount=1,
        )
    )
    db.commit()

    result = rebuild_cash_statements(db)

    assert (result.plan.inserted, result.plan.updated, result.plan.deleted) == (0, 1, 3)
    assert lines(db) == [
        ("2026-01-11", "debit", Decimal("750000"), "Feed purchase Ayam"),
        ("2026-01-16", "credit", Decimal("400000"), "Fish sale"),
    ]
    assert next(row.id for row in statements(db) if row.source_id == legacy_history["feed"]) == feed_row
    assert rebuild_cash_statements(db).plan.changed is False


def test_cli(db: Session, legacy_history: dict[str, int], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Statement rows to insert: 4" in output
    assert "Net (credit - debit):" in output and "-400100" in output
    assert "Dry run: nothing was written." in output
    assert statements(db) == []

    assert main([]) == 0
    assert "Cash statements updated." in capsys.readouterr().out
    assert len(statements(db)) == 4

    assert main([]) == 0
    assert "already up to date" in capsys.readouterr().out


# --- Constraints --------------------------------------------------------------------------------

VALID_ROW: dict[str, object] = {
    "statement_date": TODAY,
    "direction": "credit",
    "amount": 1,
    "category": "OTHER",
    "description": "x",
    "reference": "revenues:1",
    "source_table": "revenues",
    "source_id": 1,
    "source_line": "paid",
    "source_transaction_date": TODAY,
    "source_amount": 1,
}


@pytest.mark.parametrize(
    "overrides",
    [
        {"direction": "in"},
        {"amount": 0},
        {"source_table": "financial_opening_balances"},
        {"payment_id": 5},  # a payment row needs its sequence
    ],
)
def test_constraints(db: Session, overrides: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        db.execute(insert(CashStatement).values(VALID_ROW | overrides))
    db.rollback()


def test_one_row_per_source_line(db: Session) -> None:
    db.execute(insert(CashStatement).values(VALID_ROW))
    db.execute(insert(CashStatement).values(VALID_ROW | {"source_line": "fee"}))
    with pytest.raises(IntegrityError):
        db.execute(insert(CashStatement).values(VALID_ROW))
    db.rollback()


# --- API ----------------------------------------------------------------------------------------


@pytest.fixture
def client(actor: User, actor_password: str) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        login = test_client.post(
            "/api/v1/auth/login",
            headers={"X-FM-API-Key": os.environ["API_KEY"]},
            json={"email": actor.email, "password": actor_password},
        )
        assert login.status_code == 200
        test_client.headers.update(
            {
                "X-FM-API-Key": os.environ["API_KEY"],
                "Authorization": f"Bearer {login.json()['data']['access_token']}",
            }
        )
        yield test_client


def test_api_statement_with_running_balance_details_and_filters(
    db: Session, legacy_history: dict[str, int], client: TestClient
) -> None:
    rebuild_cash_statements(db)

    response = client.get("/api/v1/cash-statements")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == 4
    # Newest first, each row with the running balance after it.
    assert [(row["statement_date"], row["debit"], row["credit"], row["balance"]) for row in body["data"]] == [
        ("2026-01-20", "100", None, "-400100"),
        ("2026-01-16", None, "400000", "-400000"),
        ("2026-01-12", "100000", None, "-800000"),
        ("2026-01-11", "700000", None, "-700000"),
    ]
    partial = body["data"][2]
    assert partial["description"] == "Utility (partial payment 1)"
    assert partial["reference"] == f"operational_cashflows:{legacy_history['utility']}"
    assert partial["source"] == {
        "table": "operational_cashflows",
        "id": legacy_history["utility"],
        "line": f"payment:{legacy_history['paid_payment']}",
        "payment_id": legacy_history["paid_payment"],
        "payment_sequence": 1,
        "transaction_date": "2026-01-10",
        "amount": "300000",
        "payment_status": "PARTIALLY_PAID",
    }

    # The balance is the statement's true running balance even when the list is filtered.
    sale = client.get("/api/v1/cash-statements", params={"category": "FISH_SALE"}).json()["data"]
    assert [(row["credit"], row["balance"], row["counterparty"]) for row in sale] == [
        ("400000", "-400000", "Agen Jaya")
    ]

    for params, expected_total in (
        ({"direction": "credit"}, 1),
        ({"direction": "debit"}, 3),
        ({"reference": f"operational_cashflows:{legacy_history['utility']}"}, 1),
        ({"source_table": "revenues"}, 2),
        ({"source_table": "revenues", "source_id": legacy_history["transfer"]}, 1),
        ({"from": "2026-01-12", "to": "2026-01-16"}, 2),
        ({"category": "TRANSFER_FEE"}, 1),
    ):
        assert client.get("/api/v1/cash-statements", params=params).json()["meta"]["total"] == expected_total, params

    paged = client.get("/api/v1/cash-statements", params={"per_page": 3, "page": 2}).json()
    assert paged["meta"] == {"page": 2, "per_page": 3, "total": 4, "pages": 2}
    assert len(paged["data"]) == 1

    assert client.get("/api/v1/cash-statements", params={"source_id": 1}).status_code == 422
    assert client.get("/api/v1/cash-statements", params={"source_table": "customers"}).status_code == 422


def test_api_writes_reach_the_statement_only_after_the_nightly_sync(db: Session, client: TestClient) -> None:
    base = "/api/v1/operational-cashflows"
    created = client.post(
        base,
        json={
            "cashflow_type": "UTILITY",
            "transaction_date": TODAY.isoformat(),
            "amount_idr": "90000",
            "details": {"description": "Internet"},
        },
    )
    cashflow_id = created.json()["data"]["id"]
    assert client.post(f"{base}/{cashflow_id}/submit").status_code == 200
    assert client.post(f"{base}/{cashflow_id}/decision", json={"decision": "APPROVED"}).status_code == 200
    partial = client.post(
        f"{base}/{cashflow_id}/payments",
        json={"payment_mode": "PARTIAL", "paid_date": TODAY.isoformat(), "amount_idr": "30000"},
    )
    assert partial.status_code == 201
    assert client.post(f"{base}/{cashflow_id}/payments", json={"paid_date": TODAY.isoformat()}).status_code == 201
    assert (
        client.post(
            "/api/v1/main-cash-account/adjustments",
            json={"direction": "credit", "amount": "2000000", "note": "Opening"},
        ).status_code
        == 201
    )

    # The writes above already moved cash_on_hand (kept current synchronously)...
    assert client.get("/api/v1/operational-cashflows/cash-on-hand").json()["data"]["amount"] == "-90000"
    # ...but cash_statements has nothing yet: it is refreshed once a night, not on every write.
    assert client.get("/api/v1/cash-statements").json()["data"] == []

    sync(db)  # stands in for the nightly app.scripts.sync_cash_statements job

    rows = client.get("/api/v1/cash-statements").json()["data"]
    assert sorted((row["description"], row["direction"], row["amount"]) for row in rows) == [
        ("Main account adjustment: Opening", "credit", "2000000"),
        ("Utility: Internet (final payment 2)", "debit", "60000"),
        ("Utility: Internet (partial payment 1)", "debit", "30000"),
    ]
