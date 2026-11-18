import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

# A log reference is "<source table>:<source id>:<action>" for automatic entries and "manual:<note>"
# otherwise; these expressions extract the source of an entry inside the database.
SOURCE_REFERENCE_PATTERN = "^(operational_cashflows|revenues|loan_repayments):[0-9]+:"
SOURCE_TABLE_SQL = f"CASE WHEN reference ~ '{SOURCE_REFERENCE_PATTERN}' THEN split_part(reference, ':', 1) END"
SOURCE_ID_SQL = f"CASE WHEN reference ~ '{SOURCE_REFERENCE_PATTERN}' THEN split_part(reference, ':', 2)::bigint END"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('ADMIN','SUPERADMIN')", name="role"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="ADMIN", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthLoginAttempt(Base):
    __tablename__ = "auth_login_attempts"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FinancialOpeningBalance(TimestampMixin, Base):
    __tablename__ = "financial_opening_balances"
    __table_args__ = (
        CheckConstraint("account IN ('OPERATIONAL','REVENUE')", name="account"),
        CheckConstraint("amount_idr >= 0", name="amount"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (CheckConstraint("customer_type IN ('WARUNG','AGEN')", name="type"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str] = mapped_column(String(50), index=True)
    customer_type: Mapped[str] = mapped_column(String(20), index=True)
    notes: Mapped[str | None] = mapped_column(Text)


class OperationalCashflow(TimestampMixin, Base):
    __tablename__ = "operational_cashflows"
    __table_args__ = (
        CheckConstraint(
            "cashflow_type IN ('FEED_AYAM','FEED_LELE','SALARY','UTILITY','HEALTH','OTHER','LELE_SEED_PURCHASE','PULLET_PURCHASE','BANK_ADMIN_FEE','MAIN_ACCOUNT_TOPUP')",
            name="type",
        ),
        CheckConstraint(
            "(cashflow_type='MAIN_ACCOUNT_TOPUP' AND direction='INFLOW') OR (cashflow_type<>'MAIN_ACCOUNT_TOPUP' AND direction='OUTFLOW')",
            name="direction",
        ),
        CheckConstraint(
            "approval_status IN ('NOT_REQUIRED','DRAFT','SUBMITTED','APPROVED','REJECTED')", name="approval"
        ),
        CheckConstraint("payment_status IN ('UNPAID','PARTIALLY_PAID','PAID')", name="payment"),
        CheckConstraint("amount_idr >= 0", name="amount"),
        # Restrict the main cash account audit to top-ups instead of the whole table. The predicate
        # covers only the type so rows that stop qualifying (e.g. unpaid again) stay visible to it.
        Index(
            "ix_operational_cashflows_main_topup",
            "id",
            postgresql_where=text("cashflow_type = 'MAIN_ACCOUNT_TOPUP'"),
        ),
        Index(
            "ix_operational_cashflows_main_topup_updated",
            "updated_at",
            "id",
            postgresql_where=text("cashflow_type = 'MAIN_ACCOUNT_TOPUP'"),
        ),
        # Lets the nightly cash statement sync find the cashflows changed during a month.
        Index("ix_operational_cashflows_updated_at", "updated_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cashflow_type: Mapped[str] = mapped_column(String(40), index=True)
    direction: Mapped[str] = mapped_column(String(10), index=True)
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    approval_status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="UNPAID", index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    submitted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    rejected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    paid_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    proof_path: Mapped[str | None] = mapped_column(Text)
    proof_url: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    operational_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    feed_details: Mapped[list[OperationalCashflowFeedDetail]] = relationship(cascade="all, delete-orphan")
    payments: Mapped[list[OperationalCashflowPayment]] = relationship(cascade="all, delete-orphan")


class OperationalCashflowFeedDetail(TimestampMixin, Base):
    __tablename__ = "operational_cashflow_feed_details"
    __table_args__ = (
        CheckConstraint("purchase_unit IN ('KG','SAK')", name="unit"),
        CheckConstraint("quantity_kg >= 0 AND price_per_kg_idr >= 0 AND line_amount_idr >= 0", name="amounts"),
        CheckConstraint(
            "(purchase_unit='KG' AND purchase_quantity >= 0 AND kg_per_sak IS NULL) OR (purchase_unit='SAK' AND purchase_quantity > 0 AND purchase_quantity = TRUNC(purchase_quantity) AND kg_per_sak > 0)",
            name="shape",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    operational_cashflow_id: Mapped[int] = mapped_column(ForeignKey("operational_cashflows.id", ondelete="CASCADE"))
    feed_type_id: Mapped[int] = mapped_column(BigInteger, index=True)
    feed_type_code: Mapped[str] = mapped_column(String(60))
    feed_type_name: Mapped[str] = mapped_column(String(255))
    feed_species: Mapped[str] = mapped_column(String(10))
    purchase_unit: Mapped[str] = mapped_column(String(10))
    purchase_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    kg_per_sak: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    price_per_purchase_unit_idr: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    quantity_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    price_per_kg_idr: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    line_amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    received_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class _OpDetailMixin(TimestampMixin):
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    operational_cashflow_id: Mapped[int] = mapped_column(
        ForeignKey("operational_cashflows.id", ondelete="CASCADE"), unique=True
    )


class OperationalCashflowSalaryDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_salary_details"
    employee_id: Mapped[int] = mapped_column(BigInteger, index=True)
    employee_name: Mapped[str] = mapped_column(String(255))
    salary_period: Mapped[str] = mapped_column(String(40))


class OperationalCashflowUtilityDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_utility_details"
    description: Mapped[str] = mapped_column(Text)


class OperationalCashflowHealthDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_health_details"
    __table_args__ = (CheckConstraint("species IN ('AYAM','LELE')", name="species"),)
    species: Mapped[str] = mapped_column(String(10))
    description: Mapped[str] = mapped_column(Text)


class OperationalCashflowOtherDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_other_details"
    description: Mapped[str] = mapped_column(Text)


class OperationalCashflowTopupDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_topup_details"
    notes: Mapped[str | None] = mapped_column(Text)


class OperationalCashflowLivestockPurchaseDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_livestock_purchase_details"
    __table_args__ = (CheckConstraint("species IN ('AYAM','LELE')", name="species"),)
    species: Mapped[str] = mapped_column(String(10), index=True)
    notes: Mapped[str | None] = mapped_column(Text)


class OperationalCashflowBankAdminDetail(_OpDetailMixin, Base):
    __tablename__ = "operational_cashflow_bank_admin_details"
    notes: Mapped[str | None] = mapped_column(Text)


class OperationalCashflowPayment(TimestampMixin, Base):
    __tablename__ = "operational_cashflow_payments"
    __table_args__ = (
        # Lets the nightly cash statement sync find the payments recorded/voided during a month.
        Index("ix_operational_cashflow_payments_updated_at", "updated_at"),
        CheckConstraint("payment_mode IN ('PARTIAL','FULL')", name="mode"),
        CheckConstraint("amount_idr >= 0", name="amount"),
        CheckConstraint(
            "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL) OR (voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)",
            name="void",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    operational_cashflow_id: Mapped[int] = mapped_column(
        ForeignKey("operational_cashflows.id", ondelete="CASCADE"), index=True
    )
    paid_date: Mapped[date] = mapped_column(Date, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    payment_mode: Mapped[str] = mapped_column(String(20))
    proof_path: Mapped[str | None] = mapped_column(Text)
    proof_url: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    void_reason: Mapped[str | None] = mapped_column(Text)


class OperationalCashflowBalance(Base):
    __tablename__ = "operational_cashflow_balances"
    __table_args__ = (CheckConstraint("type IN ('cash_on_hand','waiting_approval','approved_unpaid')", name="type"),)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    type: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"), server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MainCashAccount(Base):
    """The single main cash account. Exactly one row may exist (enforced by the singleton index)."""

    __tablename__ = "main_cash_accounts"
    __table_args__ = (Index("uq_main_cash_accounts_singleton", text("(true)"), unique=True),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    current_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"), server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MainCashAccountLog(Base):
    """Immutable ledger of every movement of the main cash account. ``credit`` adds to
    ``current_amount`` and ``debit`` subtracts from it, so the signed sum of the log always equals
    the account's ``current_amount``.
    """

    __tablename__ = "main_cash_account_logs"
    __table_args__ = (
        CheckConstraint("direction IN ('credit','debit')", name="direction"),
        CheckConstraint("amount > 0", name="amount"),
        # Lets the audit look up "what has already been logged for this source row".
        Index(
            "ix_main_cash_account_logs_source",
            "source_table",
            "source_id",
            postgresql_where=text("source_table IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    main_cash_account_id: Mapped[int] = mapped_column(
        ForeignKey("main_cash_accounts.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    direction: Mapped[str] = mapped_column(String(10), index=True)
    reference: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # Derived from ``reference`` by the database so the audit can join and index the source of an
    # entry without parsing text per row. NULL for manual adjustments.
    source_table: Mapped[str | None] = mapped_column(Text, Computed(SOURCE_TABLE_SQL, persisted=True))
    source_id: Mapped[int | None] = mapped_column(BigInteger, Computed(SOURCE_ID_SQL, persisted=True))


class MainCashAccountEvent(Base):
    """Outbox of main cash account movements that are committed but not posted to the log yet.

    Cashflow/revenue transactions only insert a row here (no lock on the account row); a
    background worker turns each row into a ``main_cash_account_logs`` entry, moves
    ``current_amount`` and deletes the row, so the table only ever holds the backlog.
    """

    __tablename__ = "main_cash_account_events"
    __table_args__ = (
        CheckConstraint("source_table IN ('operational_cashflows','revenues','loan_repayments')", name="source"),
        CheckConstraint("delta <> 0", name="delta"),
        Index("ix_main_cash_account_events_source", "source_table", "source_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_table: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(100))
    # Signed: positive = credit, negative = debit.
    delta: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MainCashAccountAuditRun(Base):
    """One execution of the daily main cash account audit (see app/scripts/audit_main_cash_account.py).

    The most recent successful run is also the watermark an incremental run starts from, and its
    ``log_high_id``/``new_amount`` let the next run add up only the log entries written since.
    """

    __tablename__ = "main_cash_account_audit_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('full','incremental')", name="mode"),
        CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED','SKIPPED')", name="status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sources_checked: Mapped[int] = mapped_column(BigInteger, default=0)
    batches: Mapped[int] = mapped_column(Integer, default=0)
    discrepancies: Mapped[int] = mapped_column(BigInteger, default=0)
    entries_posted: Mapped[int] = mapped_column(BigInteger, default=0)
    # Pending outbox events the run posted before auditing (normally 0: the worker keeps up).
    events_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_posted: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    debit_posted: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    previous_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 0))
    new_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 0))
    log_high_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)


class CashStatementSyncRun(Base):
    """One execution of the nightly cash statement sync (see app/scripts/sync_cash_statements.py).

    History for maintainers only (no API): read it straight from the database, e.g. to check that
    every night ran, what each run changed, or why one failed. Written in its own transaction
    before the sync starts (``RUNNING``) and updated when it ends, so a failed run is kept too.
    A row left in ``RUNNING`` means the process died mid-run.
    """

    __tablename__ = "cash_statement_sync_runs"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED')", name="status"),
        CheckConstraint("period_end >= period_start", name="period"),
        Index("ix_cash_statement_sync_runs_period", "period_start", "started_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The calendar month synced (first and last day, in APP_TIMEZONE).
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    # Source changes made from this moment on were in scope (the month plus a one-day overlap).
    changed_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Source transactions re-derived, per source table and in total.
    cashflows_checked: Mapped[int] = mapped_column(Integer, default=0)
    revenues_checked: Mapped[int] = mapped_column(Integer, default=0)
    adjustments_checked: Mapped[int] = mapped_column(Integer, default=0)
    loans_checked: Mapped[int] = mapped_column(Integer, default=0)
    transactions_checked: Mapped[int] = mapped_column(Integer, default=0)
    # Statement rows written (for a dry run: that would have been written).
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0)
    rows_deleted: Mapped[int] = mapped_column(Integer, default=0)
    # The month's statement once synced: row count and money in/out.
    period_rows: Mapped[int] = mapped_column(Integer, default=0)
    period_credit: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    period_debit: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    error: Mapped[str | None] = mapped_column(Text)


class CashStatement(TimestampMixin, Base):
    """One real cash movement of the business, bank-statement style (see cash_statement_service).

    Derived read model built from three sources, never edited by hand:

    - ``operational_cashflows`` (every type except ``MAIN_ACCOUNT_TOPUP``): a **debit** per amount
      actually paid, once the cashflow is ``PARTIALLY_PAID``/``PAID``;
    - ``revenues`` (every type except ``MAIN_ACCOUNT_TRANSFER``): a **credit** per amount actually
      received, once the revenue is ``PARTIALLY_PAID``/``PAID``; for a paid
      ``MAIN_ACCOUNT_TRANSFER`` only its transfer fee, as a **debit**;
    - ``main_cash_account_logs``: the manual adjustments only, in their own direction.

    A transaction paid in several parts has one row per payment, all with the same ``reference``.
    """

    __tablename__ = "cash_statements"
    __table_args__ = (
        CheckConstraint("direction IN ('credit','debit')", name="direction"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint(
            "source_table IN ('operational_cashflows','revenues','main_cash_account_logs','loans')", name="source"
        ),
        CheckConstraint("(payment_id IS NULL) = (payment_sequence IS NULL)", name="payment"),
        # One source transaction produces one row per payment (or one 'paid'/'fee'/'manual' row):
        # source_line tells them apart and makes the sync idempotent.
        UniqueConstraint("source_table", "source_id", "source_line"),
        # Statement order and running balance.
        Index("ix_cash_statements_date_id", "statement_date", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The date the money actually moved: the payment date, the paid date of a transaction settled
    # without payment rows (direct transfer, transfer fee), or the posting date of an adjustment.
    statement_date: Mapped[date] = mapped_column(Date)
    # credit = money in, debit = money out.
    direction: Mapped[str] = mapped_column(String(10), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    # The cashflow/revenue type, TRANSFER_FEE, MANUAL_ADJUSTMENT or LOAN_REPAYMENT.
    category: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str] = mapped_column(Text)
    # Customer of a sale or employee of a salary, when known.
    counterparty: Mapped[str | None] = mapped_column(String(255))
    # "<source_table>:<source_id>": the same for every row of one transaction (e.g. its partial payments).
    reference: Mapped[str] = mapped_column(String(80), index=True)
    source_table: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[int] = mapped_column(BigInteger)
    # "payment:<payment id>", "paid", "fee", "manual" or "repayment:<repayment id>".
    source_line: Mapped[str] = mapped_column(String(60))
    # The operational_cashflow_payments / revenue_payments / loan_repayments row and its number
    # within the transaction.
    payment_id: Mapped[int | None] = mapped_column(BigInteger)
    payment_sequence: Mapped[int | None] = mapped_column(Integer)
    # Details of the source transaction as it is now.
    source_transaction_date: Mapped[date] = mapped_column(Date)
    source_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    source_payment_status: Mapped[str | None] = mapped_column(String(20))


class Revenue(TimestampMixin, Base):
    __tablename__ = "revenues"
    __table_args__ = (
        CheckConstraint("revenue_type IN ('EGG_SALE','FISH_SALE','OTHER','MAIN_ACCOUNT_TRANSFER')", name="type"),
        CheckConstraint(
            "(revenue_type='MAIN_ACCOUNT_TRANSFER' AND direction='OUTFLOW') OR (revenue_type<>'MAIN_ACCOUNT_TRANSFER' AND direction='INFLOW')",
            name="direction",
        ),
        CheckConstraint(
            "approval_status IN ('NOT_REQUIRED','DRAFT','SUBMITTED','APPROVED','REJECTED')", name="approval"
        ),
        CheckConstraint("payment_status IN ('UNPAID','PARTIALLY_PAID','PAID')", name="payment"),
        CheckConstraint("amount_idr >= 0", name="amount"),
        # Same idea as the top-up indexes above, for main account transfers.
        Index(
            "ix_revenues_main_transfer",
            "id",
            postgresql_where=text("revenue_type = 'MAIN_ACCOUNT_TRANSFER'"),
        ),
        Index(
            "ix_revenues_main_transfer_updated",
            "updated_at",
            "id",
            postgresql_where=text("revenue_type = 'MAIN_ACCOUNT_TRANSFER'"),
        ),
        # Lets the nightly cash statement sync find the revenues changed during a month.
        Index("ix_revenues_updated_at", "updated_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_type: Mapped[str] = mapped_column(String(30), index=True)
    direction: Mapped[str] = mapped_column(String(10), index=True)
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    approval_status: Mapped[str] = mapped_column(String(20), default="NOT_REQUIRED", index=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="UNPAID", index=True)
    payment_method: Mapped[str | None] = mapped_column(String(20), index=True)
    direct_transfer_date: Mapped[date | None] = mapped_column(Date)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    submitted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    rejected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    paid_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    proof_path: Mapped[str | None] = mapped_column(Text)
    proof_url: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    operational_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    customer: Mapped[Customer | None] = relationship()
    egg_details: Mapped[list[RevenueEggDetail]] = relationship(cascade="all, delete-orphan")
    fish_detail: Mapped[RevenueFishDetail | None] = relationship(cascade="all, delete-orphan")
    payments: Mapped[list[RevenuePayment]] = relationship(cascade="all, delete-orphan")


class RevenueEggDetail(TimestampMixin, Base):
    __tablename__ = "revenue_egg_details"
    __table_args__ = (
        UniqueConstraint("revenue_id", "egg_category"),
        CheckConstraint("egg_category IN ('UTUH','PECAH','REJECT')", name="category"),
        CheckConstraint(
            "weight_kg >= 0 AND quantity_pieces >= 0 AND price_per_kg_idr >= 0 AND line_amount_idr >= 0", name="amounts"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"))
    egg_category: Mapped[str] = mapped_column(String(10), index=True)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    quantity_pieces: Mapped[int] = mapped_column(BigInteger)
    price_per_kg_idr: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    line_amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    notes: Mapped[str | None] = mapped_column(Text)


class RevenueFishDetail(TimestampMixin, Base):
    __tablename__ = "revenue_fish_details"
    __table_args__ = (CheckConstraint("weight_kg >= 0 AND price_per_kg_idr >= 0", name="amounts"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"), unique=True)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    price_per_kg_idr: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    notes: Mapped[str | None] = mapped_column(Text)


class RevenueOtherDetail(TimestampMixin, Base):
    __tablename__ = "revenue_other_details"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"), unique=True)
    notes: Mapped[str | None] = mapped_column(Text)


class RevenueTransferDetail(TimestampMixin, Base):
    __tablename__ = "revenue_transfer_details"
    __table_args__ = (
        CheckConstraint(
            "gross_amount_idr >= 0 AND payment_cost_idr >= 0 AND payment_cost_idr <= gross_amount_idr", name="amounts"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"), unique=True)
    gross_amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    payment_cost_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    notes: Mapped[str | None] = mapped_column(Text)


class RevenuePayment(TimestampMixin, Base):
    __tablename__ = "revenue_payments"
    __table_args__ = (
        # Lets the nightly cash statement sync find the payments recorded/voided during a month.
        Index("ix_revenue_payments_updated_at", "updated_at"),
        CheckConstraint("payment_mode IN ('PARTIAL','FULL')", name="mode"),
        CheckConstraint("amount_idr >= 0", name="amount"),
        CheckConstraint(
            "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL) OR (voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)",
            name="void",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revenue_id: Mapped[int] = mapped_column(ForeignKey("revenues.id", ondelete="CASCADE"), index=True)
    paid_date: Mapped[date] = mapped_column(Date, index=True)
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    payment_mode: Mapped[str] = mapped_column(String(20))
    proof_path: Mapped[str | None] = mapped_column(Text)
    proof_url: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    void_reason: Mapped[str | None] = mapped_column(Text)


class Lender(TimestampMixin, Base):
    """A bank, cooperative or person the business borrows from. Only the name is required."""

    __tablename__ = "lenders"
    __table_args__ = (
        # "Bank BRI" and "bank bri" are the same lender.
        Index("uq_lenders_name_lower", text("lower(name)"), unique=True),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    contact_person: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # An inactive lender keeps its loans but cannot receive new ones.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class Loan(TimestampMixin, Base):
    """A loan taken by the business: its terms and, in ``installments``, its repayment schedule.

    - ``SHORT_TERM``: a single installment due on ``first_due_date`` (any date from the start, e.g.
      one week later), with a flat ``interest_amount`` in IDR (0 allowed) instead of a rate.
    - ``LONG_TERM``: ``term_months`` monthly installments from ``first_due_date``, with interest from
      ``annual_interest_rate`` and ``interest_method``.
    """

    __tablename__ = "loans"
    __table_args__ = (
        CheckConstraint("loan_type IN ('SHORT_TERM','LONG_TERM')", name="type"),
        CheckConstraint(
            "(loan_type = 'SHORT_TERM' AND interest_amount IS NOT NULL AND annual_interest_rate IS NULL"
            " AND interest_method IS NULL AND term_months IS NULL)"
            " OR (loan_type = 'LONG_TERM' AND interest_amount IS NULL AND annual_interest_rate IS NOT NULL"
            " AND interest_method IS NOT NULL AND term_months IS NOT NULL)",
            name="shape",
        ),
        CheckConstraint("interest_method IN ('FLAT','ANNUITY')", name="method"),
        CheckConstraint("status IN ('ACTIVE','PAID_OFF','CANCELLED')", name="status"),
        CheckConstraint("principal_amount > 0", name="principal"),
        CheckConstraint("interest_amount >= 0", name="interest"),
        CheckConstraint("annual_interest_rate >= 0 AND annual_interest_rate <= 100", name="rate"),
        CheckConstraint("term_months BETWEEN 1 AND 600", name="term"),
        CheckConstraint("first_due_date >= start_date", name="dates"),
        CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)"
            " AND (cancelled_at IS NULL) = (cancelled_by IS NULL)"
            " AND (cancelled_at IS NULL) = (cancel_reason IS NULL)",
            name="cancel",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lender_id: Mapped[int] = mapped_column(ForeignKey("lenders.id", ondelete="RESTRICT"), index=True)
    # The lender's contract/account number.
    reference_number: Mapped[str | None] = mapped_column(String(100))
    loan_type: Mapped[str] = mapped_column(String(20), index=True)
    principal_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    # SHORT_TERM only: the flat interest in IDR, entered as an amount (0 allowed).
    interest_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 0))
    # LONG_TERM only: percent per year (e.g. 11.5 for 11.5 %), FLAT (interest on the original
    # principal every month) or ANNUITY (equal installments on the declining balance), and the
    # number of monthly installments.
    annual_interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    interest_method: Mapped[str | None] = mapped_column(String(10))
    term_months: Mapped[int | None] = mapped_column(Integer)
    # Date the loan was received/signed, and due date of the (first) installment.
    start_date: Mapped[date] = mapped_column(Date, index=True)
    first_due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    installments: Mapped[list[LoanInstallment]] = relationship(
        cascade="all, delete-orphan", order_by="LoanInstallment.sequence"
    )
    repayments: Mapped[list[LoanRepayment]] = relationship(order_by="(LoanRepayment.paid_date, LoanRepayment.id)")
    lender: Mapped[Lender] = relationship()


class LoanInstallment(TimestampMixin, Base):
    """One monthly installment of a loan's schedule and how much of it has been repaid.

    The schedule is generated from the loan terms; the paid amounts are re-derived from the
    non-voided repayments after every repayment or void (oldest installment first, interest before
    principal)."""

    __tablename__ = "loan_installments"
    __table_args__ = (
        UniqueConstraint("loan_id", "sequence"),
        CheckConstraint("status IN ('UNPAID','PARTIALLY_PAID','PAID')", name="status"),
        CheckConstraint(
            "principal_amount >= 0 AND interest_amount >= 0 AND principal_paid >= 0 AND interest_paid >= 0"
            " AND principal_paid <= principal_amount AND interest_paid <= interest_amount",
            name="amounts",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    principal_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    interest_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    principal_paid: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    interest_paid: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(20), default="UNPAID", index=True)
    # Date of the repayment that settled it.
    paid_date: Mapped[date | None] = mapped_column(Date)


class LoanRepayment(TimestampMixin, Base):
    """Money paid back on a loan, out of the main cash account.

    ``principal_amount`` + ``interest_amount`` = ``amount``: how the repayment was split, and the
    installments it went to, are derived by the allocation (see loan_service) and refreshed after
    every void or loan edit. The same split is kept per installment
    (``loan_installments.principal_paid`` / ``interest_paid``)."""

    __tablename__ = "loan_repayments"
    __table_args__ = (
        CheckConstraint(
            "amount > 0 AND principal_amount >= 0 AND interest_amount >= 0"
            " AND principal_amount + interest_amount = amount",
            name="amounts",
        ),
        CheckConstraint(
            "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL)"
            " OR (voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)",
            name="void",
        ),
        # Lets the main cash account audit run incrementally over repayments.
        Index("ix_loan_repayments_updated", "updated_at", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id", ondelete="RESTRICT"), index=True)
    paid_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    principal_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    interest_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    # Sequences of the first and last installment this repayment went to.
    first_installment: Mapped[int | None] = mapped_column(Integer)
    last_installment: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    void_reason: Mapped[str | None] = mapped_column(Text)


# --- Monthly cut-off ----------------------------------------------------------------------------
# The month-end close for the finance team (app.features.monthly_cutoff_service), aggregated from
# cash_statements. Every run is a new, immutable snapshot; ``is_latest`` marks the one to report
# for a month (a re-run after late entries replaces it without losing the earlier figures).
# Source ids in the detail tables are plain references (no foreign keys) and names are copied, so
# a snapshot never changes after it is taken.


class MonthlyCutoff(Base):
    """One month-end close: its status and the month's headline figures.

    Cash basis: every amount comes from money that actually moved (``cash_statements``), except
    the opening balances entered at go-live and the loan interest/principal split, which are not
    on the statement (see the service for how double counting is avoided).
    """

    __tablename__ = "monthly_cutoffs"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED')", name="status"),
        CheckConstraint("period_end >= period_start", name="period"),
        CheckConstraint("NOT is_latest OR status = 'SUCCESS'", name="latest"),
        # At most one reported snapshot per month.
        Index("uq_monthly_cutoffs_latest", "period_start", unique=True, postgresql_where=text("is_latest")),
        Index("ix_monthly_cutoffs_period", "period_start", "started_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), index=True)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The cash statement sync of the month run just before aggregating.
    sync_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("cash_statement_sync_runs.id", ondelete="SET NULL"), index=True
    )
    error: Mapped[str | None] = mapped_column(Text)
    # Consistency checks that did not hold (the figures are still saved), as a list of strings.
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list)

    # Actual cash: cash_opening + opening_balances_in_period + cash_in - cash_out = cash_closing.
    cash_opening: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    opening_balances_in_period: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    cash_in: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    cash_out: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    cash_closing: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    # Part of cash_closing that is not on the statement: financial_opening_balances up to period_end.
    opening_balances_total: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    statement_rows: Mapped[int] = mapped_column(Integer, default=0)

    # Profit and loss (cash basis).
    revenue: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    operating_expenses: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    other_expenses: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    finance_costs: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    net_profit: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))

    # Loans at period_end.
    loans_outstanding: Mapped[int] = mapped_column(Integer, default=0)
    loan_principal_outstanding: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    loan_interest_outstanding: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    loan_overdue_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    loan_principal_repaid: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    loan_interest_repaid: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))

    # Receivables at period_end.
    receivable_invoices: Mapped[int] = mapped_column(Integer, default=0)
    receivables: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))
    receivables_pending_verification: Mapped[Decimal] = mapped_column(Numeric(18, 0), default=Decimal("0"))

    # Potential revenue at period_end (see month_end_assets): the month-end egg stock and fish
    # weight estimate from operational-service, valued at the month's sales prices. Not cash and
    # not in the P&L. NULL on snapshots taken before it was part of the cut-off.
    potential_revenue: Mapped[Decimal | None] = mapped_column(Numeric(18, 0))
    potential_revenue_eggs: Mapped[Decimal | None] = mapped_column(Numeric(18, 0))
    potential_revenue_fish: Mapped[Decimal | None] = mapped_column(Numeric(18, 0))
    # The operational data used, per dataset: {"egg_stock": {"revision", "finalized_at"}, ...}.
    month_end_sources: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class MonthlyCutoffCategory(Base):
    """The month's statement rows grouped by direction, source and category."""

    __tablename__ = "monthly_cutoff_categories"
    __table_args__ = (
        CheckConstraint("direction IN ('credit','debit')", name="direction"),
        UniqueConstraint("cutoff_id", "direction", "source_table", "category"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cutoff_id: Mapped[int] = mapped_column(ForeignKey("monthly_cutoffs.id", ondelete="CASCADE"))
    direction: Mapped[str] = mapped_column(String(10))
    source_table: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(40))
    rows: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))


class MonthlyCutoffProfitLoss(Base):
    """One line of the month's profit and loss, in report order. Amounts are positive; the section
    says whether they add (``REVENUE``) or subtract. ``NON_PL`` lines are cash movements that are
    not income or expense (loan principal, manual adjustments), listed so the P&L reconciles with
    the cash movement: net_profit - loan principal + manual in - manual out = cash_in - cash_out.
    """

    __tablename__ = "monthly_cutoff_profit_loss"
    __table_args__ = (
        CheckConstraint(
            "section IN ('REVENUE','OPERATING_EXPENSE','OTHER_EXPENSE','FINANCE_COST','NON_PL')", name="section"
        ),
        UniqueConstraint("cutoff_id", "section", "line"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cutoff_id: Mapped[int] = mapped_column(ForeignKey("monthly_cutoffs.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(20))
    line: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))


class MonthlyCutoffLoan(Base):
    """A loan owed at period_end (or repaid during the month), as of period_end."""

    __tablename__ = "monthly_cutoff_loans"
    __table_args__ = (UniqueConstraint("cutoff_id", "loan_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cutoff_id: Mapped[int] = mapped_column(ForeignKey("monthly_cutoffs.id", ondelete="CASCADE"))
    loan_id: Mapped[int] = mapped_column(BigInteger)
    lender_id: Mapped[int] = mapped_column(BigInteger)
    lender_name: Mapped[str] = mapped_column(String(255))
    reference_number: Mapped[str | None] = mapped_column(String(100))
    loan_type: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    maturity_date: Mapped[date] = mapped_column(Date)
    principal_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    principal_paid: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    principal_outstanding: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    interest_total: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    interest_paid: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    interest_outstanding: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    overdue_installments: Mapped[int] = mapped_column(Integer)
    overdue_amount: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    principal_repaid_in_period: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    interest_repaid_in_period: Mapped[Decimal] = mapped_column(Numeric(18, 0))


class MonthlyCutoffReceivable(Base):
    """What one customer still owed at period_end, with its age (days since the sale date)."""

    __tablename__ = "monthly_cutoff_receivables"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cutoff_id: Mapped[int] = mapped_column(ForeignKey("monthly_cutoffs.id", ondelete="CASCADE"), index=True)
    # NULL: sales without a customer (e.g. OTHER revenue).
    customer_id: Mapped[int | None] = mapped_column(BigInteger)
    customer_name: Mapped[str] = mapped_column(String(255))
    invoices: Mapped[int] = mapped_column(Integer)
    outstanding: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    aging_0_30: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    aging_31_60: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    aging_61_90: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    aging_over_90: Mapped[Decimal] = mapped_column(Numeric(18, 0))
    # Direct-transfer sales still waiting for approval: paid according to the customer, not
    # verified yet. Not part of ``outstanding`` or its aging.
    pending_verification: Mapped[Decimal] = mapped_column(Numeric(18, 0))


class MonthlyCutoffAsset(Base):
    """One line of the month's potential revenue: the stock of an egg category, or the estimated
    fish weight of a pond, at period_end (operational-service data, copied), and its value at the
    sales price it was priced with. A line without a price (no sale to price it from) is worth 0.
    """

    __tablename__ = "monthly_cutoff_assets"
    __table_args__ = (
        CheckConstraint("asset_type IN ('EGG_STOCK','FISH_STOCK')", name="asset_type"),
        CheckConstraint("weight_kg >= 0 AND value >= 0", name="amounts"),
        CheckConstraint(
            "(price_per_kg IS NULL) = (price_month IS NULL) AND (price_per_kg IS NULL OR price_sales > 0)",
            name="price",
        ),
        UniqueConstraint("cutoff_id", "asset_type", "item_code"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cutoff_id: Mapped[int] = mapped_column(ForeignKey("monthly_cutoffs.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    asset_type: Mapped[str] = mapped_column(String(20))
    # Egg category (UTUH, PECAH, REJECT) or pond code; the name is the pond name for fish.
    item_code: Mapped[str] = mapped_column(String(60))
    item_name: Mapped[str] = mapped_column(String(255))
    # Operational pond id (no foreign key: another service's data); NULL for eggs.
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    # Eggs in stock, or the estimated number of fish (NULL when operational does not give it).
    quantity_pieces: Mapped[int | None] = mapped_column(BigInteger)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    # Average sales price per kg and the month of the sales it comes from; NULL: not priced.
    price_per_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    price_month: Mapped[date | None] = mapped_column(Date)
    price_sales: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 0))


class MonthlyCutoffExport(Base):
    """The Excel workbook of a cut-off (app.features.monthly_cutoff_export): its generation and
    where the file is stored. One per cut-off, created by its first generation attempt.

    ``status`` is the last attempt's: ``GENERATING`` (running, or interrupted if it has been
    running longer than EXPORT_GENERATION_TIMEOUT_MINUTES), ``SUCCEEDED`` or ``FAILED`` (with
    ``last_error``). ``failed_attempts`` counts the failures in a row, automatic and manual; the
    automatic retries stop at EXPORT_MAX_AUTOMATIC_ATTEMPTS. The file columns describe the last
    successful generation (NULL until there is one): a failed regeneration keeps the earlier file.
    The file is built from the snapshot and the month's cash statement rows at generation time;
    ``warnings`` lists what did not match the snapshot (e.g. the statement changed after the close).
    """

    __tablename__ = "monthly_cutoff_exports"
    __table_args__ = (
        CheckConstraint("status IN ('GENERATING','SUCCEEDED','FAILED')", name="status"),
        CheckConstraint("failed_attempts >= 0", name="failed_attempts"),
        CheckConstraint(
            "(storage_key IS NULL) = (generated_at IS NULL)"
            " AND (storage_key IS NULL) = (size_bytes IS NULL)"
            " AND (size_bytes IS NULL OR size_bytes > 0)",
            name="file",
        ),
        CheckConstraint("status <> 'SUCCEEDED' OR storage_key IS NOT NULL", name="succeeded"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cutoff_id: Mapped[int] = mapped_column(ForeignKey("monthly_cutoffs.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    attempt_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Who asked for the last attempt from the frontend; NULL: an automatic attempt.
    attempt_requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    # The last successful generation.
    storage_backend: Mapped[str | None] = mapped_column(String(20))
    storage_key: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list)


class MonthlyCutoffJob(Base):
    """A monthly cut-off or Excel export to run in the background (app.features.monthly_cutoff_jobs).

    ``CUTOFF`` creates (or recreates) a month's cut-off, then generates its Excel export; ``EXPORT``
    generates the export of the month's reported cut-off. Queued from the frontend (``QUEUED``, run
    by the worker) or started by the hourly cron (``requested_by`` NULL). ``FAILED`` jobs say why
    (``reason_code``: OPERATIONAL_DATA_NOT_READY, INTERRUPTED, ALREADY_RUNNING, ERROR).
    At most one job of a type is queued or running per month.
    """

    __tablename__ = "monthly_cutoff_jobs"
    __table_args__ = (
        CheckConstraint("job_type IN ('CUTOFF','EXPORT')", name="job_type"),
        CheckConstraint("status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')", name="status"),
        CheckConstraint("(status = 'FAILED') = (reason_code IS NOT NULL)", name="reason"),
        Index(
            "uq_monthly_cutoff_jobs_active",
            "job_type",
            "period_start",
            unique=True,
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
        ),
        Index("ix_monthly_cutoff_jobs_queue", "status", "requested_at"),
        Index("ix_monthly_cutoff_jobs_period", "period_start", "job_type", "requested_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(10))
    period_start: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))
    # The user who asked from the frontend; NULL: the hourly cron (automatic).
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text)
    # The cut-off created (CUTOFF) or exported (EXPORT).
    cutoff_id: Mapped[int | None] = mapped_column(ForeignKey("monthly_cutoffs.id", ondelete="SET NULL"))


class OperationalIntegrationOutbox(TimestampMixin, Base):
    __tablename__ = "operational_integration_outbox"
    __table_args__ = (
        UniqueConstraint("aggregate_type", "aggregate_id", "source_revision"),
        CheckConstraint("aggregate_type IN ('FEED_CASHFLOW','EGG_REVENUE')", name="aggregate_type"),
        CheckConstraint("status IN ('PENDING','PROCESSING','DELIVERED','SUPERSEDED','FAILED')", name="status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(30), index=True)
    aggregate_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_revision: Mapped[int] = mapped_column(BigInteger)
    target_path: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(TimestampMixin, Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    entity_type: Mapped[str] = mapped_column(String(100), index=True)
    entity_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)




