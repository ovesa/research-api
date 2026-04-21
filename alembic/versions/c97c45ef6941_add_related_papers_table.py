"""add_related_papers_table

Revision ID: c97c45ef6941
Revises: e5d2a4d9f11e
Create Date: 2026-04-21 15:04:57.849388

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c97c45ef6941'
down_revision: Union[str, Sequence[str], None] = 'e5d2a4d9f11e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade():
    # Directed citation edges between papers.
    # citing_identifier  → the paper that contains the reference
    # cited_identifier   → the paper being referenced
    # source             → where the edge came from (ads, manual)
    #
    # Architecture note: we store directed edges, not undirected.
    # This means "paper A cites paper B" is stored as one row.
    # To find everything that cites paper B, query cited_identifier = B.
    # To find everything paper A cites, query citing_identifier = A.
    # The composite primary key prevents duplicate edges.
    op.create_table(
        "related_papers",
        sa.Column("citing_identifier", sa.String(), nullable=False),
        sa.Column("cited_identifier", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="ads"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("citing_identifier", "cited_identifier"),
        sa.ForeignKeyConstraint(
            ["citing_identifier"], ["papers.identifier"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_related_papers_cited_identifier",
        "related_papers",
        ["cited_identifier"],
    )


def downgrade():
    op.drop_index("ix_related_papers_cited_identifier", table_name="related_papers")
    op.drop_table("related_papers")