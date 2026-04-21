"""add_confidence_and_data_gaps_to_extractions

Revision ID: e5d2a4d9f11e
Revises: e8516ca26ce8
Create Date: 2026-04-21 14:20:56.292592

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5d2a4d9f11e'
down_revision: Union[str, Sequence[str], None] = 'e8516ca26ce8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # confidence: how reliable is this extraction overall.
    # low/medium/high based on abstract length and field completeness.
    op.add_column(
        "extractions",
        sa.Column("confidence", sa.String(), nullable=True)
    )
    # data_gaps: what the paper says is missing or unresolved.
    # JSON array matching the same pattern as open_questions.
    op.add_column(
        "extractions",
        sa.Column("data_gaps", sa.JSON(), nullable=True)
    )


def downgrade():
    op.drop_column("extractions", "confidence")
    op.drop_column("extractions", "data_gaps")