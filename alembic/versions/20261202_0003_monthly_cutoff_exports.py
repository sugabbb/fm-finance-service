"""Excel exports of the monthly cut-offs: ``monthly_cutoff_exports``.

Like 20261201_0002, skipped when the table exists already (a fresh database gets it from the
baseline's ``create_all``); the definition below matches the model exactly.

Revision ID: 20261202_0003
Revises: 20261201_0002
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261202_0003"
down_revision = "20261201_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("monthly_cutoff_exports"):
        return
    op.create_table(
        "monthly_cutoff_exports",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("cutoff_id", sa.BigInteger(), nullable=False),
        sa.Column("storage_backend", sa.String(20), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["cutoff_id"],
            ["monthly_cutoffs.id"],
            name=op.f("fk_monthly_cutoff_exports_cutoff_id_monthly_cutoffs"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_monthly_cutoff_exports_size")),
        sa.UniqueConstraint("cutoff_id", name=op.f("uq_monthly_cutoff_exports_cutoff_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monthly_cutoff_exports")),
    )


def downgrade() -> None:
    op.drop_table("monthly_cutoff_exports")
