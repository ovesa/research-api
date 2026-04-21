"""add_prompt_version_to_extractions

Revision ID: e8516ca26ce8
Revises: e4729ca00e20
Create Date: 2026-04-21 12:02:56.382429

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e8516ca26ce8"
down_revision: Union[str, Sequence[str], None] = "e4729ca00e20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column(
        "extractions", sa.Column("prompt_version", sa.String(), nullable=True)
    )


def downgrade():
    op.drop_column("extractions", "prompt_version")
