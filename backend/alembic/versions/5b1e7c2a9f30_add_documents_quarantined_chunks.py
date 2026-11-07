"""add documents.quarantined_chunks

Revision ID: 5b1e7c2a9f30
Revises: d899284d57f1
"""
import sqlalchemy as sa
from alembic import op

revision = "5b1e7c2a9f30"
down_revision = "d899284d57f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("quarantined_chunks", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("documents", "quarantined_chunks")