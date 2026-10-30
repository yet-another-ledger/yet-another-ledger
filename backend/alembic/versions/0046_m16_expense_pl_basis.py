"""Add Expense and recurring Expense P/L controls.

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("expense", "recurring_expense"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN include_in_pl BOOLEAN NOT NULL DEFAULT true"
        )
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN pl_cost_basis TEXT NOT NULL DEFAULT 'NET' "
            "CHECK (pl_cost_basis IN ('NET', 'GROSS', 'AUTO'))"
        )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM expense WHERE include_in_pl IS DISTINCT FROM true "
        "OR pl_cost_basis IS DISTINCT FROM 'NET') OR "
        "EXISTS (SELECT 1 FROM recurring_expense WHERE include_in_pl IS DISTINCT FROM true "
        "OR pl_cost_basis IS DISTINCT FROM 'NET') THEN "
        "RAISE EXCEPTION 'M16 Expense settings contain non-default values; restore them "
        "before downgrade'; END IF; END $$"
    )
    op.drop_column("recurring_expense", "pl_cost_basis")
    op.drop_column("recurring_expense", "include_in_pl")
    op.drop_column("expense", "pl_cost_basis")
    op.drop_column("expense", "include_in_pl")
