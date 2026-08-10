"""add linked_account_id to telegram/whatsapp conversations for trade-approval identity

Revision ID: 20260811_0001
Revises: 20260810_0001
Create Date: 2026-08-11

Adds a nullable linked_account_id to telegram_conversations and
whatsapp_conversations, mapping a channel identity to an authenticated
Synkora Account. Ordinary chat access to the trading agent never requires
this — it's only checked before an approve/reject action on a trade
proposal is accepted from that channel (§16/§17 of the trading domain spec).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

# revision identifiers, used by Alembic.
revision = "20260811_0001"
down_revision = "20260810_0001"
branch_labels = None
depends_on = None


def _column_exists(conn, table, column) -> bool:
    result = conn.execute(
        sa.text("SELECT 1 FROM information_schema.columns WHERE table_name=:t AND column_name=:c"),
        {"t": table, "c": column},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, "telegram_conversations", "linked_account_id"):
        op.add_column(
            "telegram_conversations",
            sa.Column(
                "linked_account_id",
                pg.UUID(as_uuid=True),
                sa.ForeignKey("accounts.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )

    if not _column_exists(conn, "whatsapp_conversations", "linked_account_id"):
        op.add_column(
            "whatsapp_conversations",
            sa.Column(
                "linked_account_id",
                pg.UUID(as_uuid=True),
                sa.ForeignKey("accounts.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn, "whatsapp_conversations", "linked_account_id"):
        op.drop_column("whatsapp_conversations", "linked_account_id")
    if _column_exists(conn, "telegram_conversations", "linked_account_id"):
        op.drop_column("telegram_conversations", "linked_account_id")
