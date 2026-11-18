"""Background jobs of the monthly cut-off: ``monthly_cutoff_jobs``.

Skipped when the table exists already (a fresh database gets it from the baseline's
``create_all``); the definition below matches the model exactly.

Revision ID: 20261204_0005
Revises: 20261203_0004
"""

import sqlalchemy as sa

from alembic import op

revision = "20261204_0005"
down_revision = "20261203_0004"
branch_labels = None
depends_on = None

TABLE = "monthly_cutoff_jobs"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("job_type", sa.String(10), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("cutoff_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_monthly_cutoff_jobs_requested_by_users"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["cutoff_id"],
            ["monthly_cutoffs.id"],
            name=op.f("fk_monthly_cutoff_jobs_cutoff_id_monthly_cutoffs"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("job_type IN ('CUTOFF','EXPORT')", name=op.f("ck_monthly_cutoff_jobs_job_type")),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')", name=op.f("ck_monthly_cutoff_jobs_status")
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (reason_code IS NOT NULL)", name=op.f("ck_monthly_cutoff_jobs_reason")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monthly_cutoff_jobs")),
    )
    op.create_index(
        "uq_monthly_cutoff_jobs_active",
        TABLE,
        ["job_type", "period_start"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"),
    )
    op.create_index("ix_monthly_cutoff_jobs_queue", TABLE, ["status", "requested_at"])
    op.create_index("ix_monthly_cutoff_jobs_period", TABLE, ["period_start", "job_type", "requested_at"])


def downgrade() -> None:
    op.drop_table(TABLE)
