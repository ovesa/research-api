"""add url column to papers table

Revision ID: bdcb8fe27cd4
Revises: 63c945ee73a9
Create Date: 2026-04-05 14:56:23.781474

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "bdcb8fe27cd4"
down_revision: Union[str, Sequence[str], None] = "63c945ee73a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    """Add url column to store the abstract page link for each paper.

    The URL is constructed from existing identifiers at fetch time.
    For arXiv papers: https://arxiv.org/abs/{arxiv_id}
    For DOI papers: https://doi.org/{doi}

    Args:
        None

    Returns:
        None
    """
    op.add_column("papers", sa.Column("url", sa.String(), nullable=True))


def downgrade():
    """Remove the url column from the papers table.

    Returns:
        None
    """
    op.drop_column("papers", "url")
