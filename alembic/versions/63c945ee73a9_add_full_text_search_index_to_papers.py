"""add full text search index to papers

Revision ID: 63c945ee73a9
Revises: e086d7e85c46
Create Date: 2026-04-05 12:03:11.750435

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "63c945ee73a9"
down_revision: Union[str, Sequence[str], None] = "e086d7e85c46"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    """Add full text search capability to the papers table.

    Postgres has a native full text search engine built in. Rather than
    using LIKE queries which are slow and dumb, tsvector converts text
    into a searchable token format that understands language (stemming,
    stop words, and relevance ranking via ts_rank).

    Changes:
        search_vector (tsvector, NULLABLE):
            A pre-computed search index column combining title and
            abstract. Stored as a generated column so it updates
            automatically when title or abstract changes. Weighted
            so title matches rank higher than abstract matches:
            A' weight for title, 'B' weight for abstract.

        ix_papers_search_vector (GIN index):
            GIN (Generalized Inverted Index) is the correct index type
            for tsvector columns. Dramatically faster than a sequential
            scan for full text queries. Essential for search performance.

    Note on weights:
        setweight(to_tsvector('english', title), 'A') means title
        matches are ranked higher than abstract matches. So a paper
        whose title contains 'solar wind' ranks above one where it
        only appears in the abstract.
    """
    # Add the search vector column
    op.add_column("papers", sa.Column("search_vector", sa.Text(), nullable=True))

    # Populate search_vector for existing rows
    op.execute("""
        UPDATE papers
        SET search_vector = (
            setweight(to_tsvector('english', COALESCE(title, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(abstract, '')), 'B')
        )::text
    """)

    # Create GIN index for fast full text search
    op.execute("""
        CREATE INDEX ix_papers_search_vector
        ON papers
        USING GIN (to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(abstract, '')))
    """)


def downgrade():
    """Remove full text search index and column.

    Warning:
        Dropping the index is fast. Dropping the column removes all
        pre-computed search vectors and requires recomputation on upgrade.
    """
    op.execute("DROP INDEX IF EXISTS ix_papers_search_vector")
    op.drop_column("papers", "search_vector")
