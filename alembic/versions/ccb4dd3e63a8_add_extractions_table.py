"""add extractions table

Revision ID: ccb4dd3e63a8
Revises: bdcb8fe27cd4
Create Date: 2026-04-11 11:56:23.699059

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ccb4dd3e63a8'
down_revision: Union[str, Sequence[str], None] = 'bdcb8fe27cd4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_table(
        "extractions",
        sa.Column("identifier", sa.String(), primary_key=True),
        sa.Column("methods", sa.JSON(), nullable=True),
        sa.Column("key_findings", sa.JSON(), nullable=True),
        sa.Column("data_type", sa.String(), nullable=True),
        sa.Column("instruments", sa.JSON(), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["identifier"], ["papers.identifier"], ondelete="CASCADE"),
    )

def downgrade():
    op.drop_table("extractions")