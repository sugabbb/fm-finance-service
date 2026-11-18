"""Potential revenue of the monthly cut-off: month-end egg stock and fish weight estimate.

Adds the potential revenue columns to ``monthly_cutoffs`` (NULL on the snapshots taken before)
and the ``monthly_cutoff_assets`` detail table.

The baseline revision creates the schema from the current models (``create_all``), so on a fresh
database these objects already exist when this revision runs: every step is skipped when its
object is there, and the definitions below match the models exactly.

Revision ID: 20261201_0002
Revises: 20261118_0001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261201_0002"
down_revision = "20261118_0001"
branch_labels = None
depends_on = None

CUTOFF_COLUMNS = (
    ("potential_revenue", sa.Numeric(18, 0)),
    ("potential_revenue_eggs", sa.Numeric(18, 0)),
    ("potential_revenue_fish", sa.Numeric(18, 0)),
    ("month_end_sources", postgresql.JSONB()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("monthly_cutoffs")}
    for name, type_ in CUTOFF_COLUMNS:
        if name not in existing:
            op.add_column("monthly_cutoffs", sa.Column(name, type_, nullable=True))

    if not inspector.has_table("monthly_cutoff_assets"):
        op.create_table(
            "monthly_cutoff_assets",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("cutoff_id", sa.BigInteger(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("asset_type", sa.String(20), nullable=False),
            sa.Column("item_code", sa.String(60), nullable=False),
            sa.Column("item_name", sa.String(255), nullable=False),
            sa.Column("source_id", sa.BigInteger(), nullable=True),
            sa.Column("quantity_pieces", sa.BigInteger(), nullable=True),
            sa.Column("weight_kg", sa.Numeric(14, 3), nullable=False),
            sa.Column("price_per_kg", sa.Numeric(18, 3), nullable=True),
            sa.Column("price_month", sa.Date(), nullable=True),
            sa.Column("price_sales", sa.Integer(), nullable=False),
            sa.Column("value", sa.Numeric(18, 0), nullable=False),
            sa.ForeignKeyConstraint(
                ["cutoff_id"],
                ["monthly_cutoffs.id"],
                name=op.f("fk_monthly_cutoff_assets_cutoff_id_monthly_cutoffs"),
                ondelete="CASCADE",
            ),
            sa.CheckConstraint(
                "asset_type IN ('EGG_STOCK','FISH_STOCK')", name=op.f("ck_monthly_cutoff_assets_asset_type")
            ),
            sa.CheckConstraint("weight_kg >= 0 AND value >= 0", name=op.f("ck_monthly_cutoff_assets_amounts")),
            sa.CheckConstraint(
                "(price_per_kg IS NULL) = (price_month IS NULL) AND (price_per_kg IS NULL OR price_sales > 0)",
                name=op.f("ck_monthly_cutoff_assets_price"),
            ),
            sa.UniqueConstraint(
                "cutoff_id", "asset_type", "item_code", name=op.f("uq_monthly_cutoff_assets_cutoff_id")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_monthly_cutoff_assets")),
        )


def downgrade() -> None:
    op.drop_table("monthly_cutoff_assets")
    for name, _ in reversed(CUTOFF_COLUMNS):
        op.drop_column("monthly_cutoffs", name)
