"""Track independent provider-side Sent-copy outcomes.

Revision ID: 0044
Revises: 0043
"""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE email_log ADD COLUMN sent_copy_status TEXT NOT NULL "
        "DEFAULT 'NOT_ATTEMPTED', ADD COLUMN sent_copy_error_code TEXT, "
        "ADD COLUMN sent_copy_saved_at TIMESTAMPTZ"
    )
    op.create_check_constraint(
        "ck_email_log_sent_copy_status",
        "email_log",
        "sent_copy_status IN ('NOT_ATTEMPTED', 'SAVED', 'FAILED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_email_log_sent_copy_status", "email_log", type_="check")
    op.drop_column("email_log", "sent_copy_saved_at")
    op.drop_column("email_log", "sent_copy_error_code")
    op.drop_column("email_log", "sent_copy_status")
