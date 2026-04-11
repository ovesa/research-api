"""remove unused columns arxiv_categories and search_vector

Revision ID: eb0e360529b0
Revises: bdcb8fe27cd4
Create Date: 2026-04-11 11:17:50.194632

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "eb0e360529b0"
down_revision: Union[str, Sequence[str], None] = "bdcb8fe27cd4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.drop_index("ix_papers_search_vector", "papers", if_exists=True)
    op.drop_column("papers", "search_vector")
    op.drop_column("papers", "arxiv_categories")


def downgrade():
    op.add_column(
        "papers",
        sa.Column("arxiv_categories", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column("papers", sa.Column("search_vector", sa.Text(), nullable=True))
