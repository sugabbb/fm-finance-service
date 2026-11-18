"""Generation status of the monthly cut-off Excel exports.

``monthly_cutoff_exports`` rows used to exist only for generated files. They now record every
attempt: status, failed attempts in a row, last error, who asked; the file columns become NULL
until a generation succeeds.

Skipped when the columns exist already (a fresh database gets them from the baseline's
``create_all``). Existing rows were all successful generations and are marked so.

Revision ID: 20261203_0004
Revises: 20261202_0003
"""

import sqlalchemy as sa

from alembic import op

revision = "20261203_0004"
down_revision = "20261202_0003"
branch_labels = None
depends_on = None

TABLE = "monthly_cutoff_exports"
FILE_COLUMNS = ("storage_backend", "storage_key", "file_name", "size_bytes", "sha256", "generated_at")
CHECKS = {
    "ck_monthly_cutoff_exports_status": "status IN ('GENERATING','SUCCEEDED','FAILED')",
    "ck_monthly_cutoff_exports_failed_attempts": "failed_attempts >= 0",
    "ck_monthly_cutoff_exports_file": (
        "(storage_key IS NULL) = (generated_at IS NULL)"
        " AND (storage_key IS NULL) = (size_bytes IS NULL)"
        " AND (size_bytes IS NULL OR size_bytes > 0)"
    ),
    "ck_monthly_cutoff_exports_succeeded": "status <> 'SUCCEEDED' OR storage_key IS NOT NULL",
}


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if "status" in columns:
        return
    op.add_column(TABLE, sa.Column("status", sa.String(20), nullable=True))
    op.add_column(TABLE, sa.Column("failed_attempts", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("attempt_finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("attempt_requested_by", sa.BigInteger(), nullable=True))
    op.execute(
        f"UPDATE {TABLE} SET status = 'SUCCEEDED', failed_attempts = 0,"
        " attempt_started_at = generated_at, attempt_finished_at = generated_at"
    )
    for name in ("status", "failed_attempts", "attempt_started_at"):
        op.alter_column(TABLE, name, nullable=False)
    for name in FILE_COLUMNS:
        op.alter_column(TABLE, name, nullable=True)
    op.drop_constraint(op.f("ck_monthly_cutoff_exports_size"), TABLE, type_="check")
    for name, condition in CHECKS.items():
        op.create_check_constraint(op.f(name), TABLE, condition)
    op.create_foreign_key(
        op.f("fk_monthly_cutoff_exports_attempt_requested_by_users"),
        TABLE,
        "users",
        ["attempt_requested_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_monthly_cutoff_exports_status"), TABLE, ["status"])


def downgrade() -> None:
    op.execute(f"DELETE FROM {TABLE} WHERE storage_key IS NULL")  # attempts without a file
    op.drop_index(op.f("ix_monthly_cutoff_exports_status"), TABLE)
    op.drop_constraint(op.f("fk_monthly_cutoff_exports_attempt_requested_by_users"), TABLE, type_="foreignkey")
    for name in CHECKS:
        op.drop_constraint(op.f(name), TABLE, type_="check")
    for name in ("status", "failed_attempts", "last_error", "attempt_started_at", "attempt_finished_at",
                 "attempt_requested_by"):
        op.drop_column(TABLE, name)
    for name in FILE_COLUMNS:
        op.alter_column(TABLE, name, nullable=False)
    op.create_check_constraint(op.f("ck_monthly_cutoff_exports_size"), TABLE, "size_bytes > 0")
