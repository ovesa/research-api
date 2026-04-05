"""create papers table

Revision ID: e086d7e85c46
Revises:
Create Date: 2026-04-05 11:42:59.299669

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e086d7e85c46"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    """Create the papers table for persistent heliophysics paper storage.

    This is the core persistence layer. Every paper successfully fetched
    and validated by the API is stored here. Redis cache sits in front
    of this — Postgres is the source of truth, Redis is the speed layer.

    Table Schema:
        identifier (str, PK):
            The DOI or arXiv ID used to look up the paper. Used as
            the primary key because it is already globally unique and
            stable. Avoids needing a separate UUID primary key.

        identifier_type (str, NOT NULL):
            Either 'doi' or 'arxiv'. Stored alongside identifier so
            queries can filter by source type.

        title (str, NOT NULL):
            Full paper title. Required — every valid paper has one.

        authors (JSON, NOT NULL):
            List of author objects with name, affiliation, orcid.
            Stored as JSON because author lists are read together,
            never queried individually by field.

        abstract (text, NULLABLE):
            Full abstract text. Nullable because CrossRef frequently
            omits abstracts for published papers.

        published_date (str, NULLABLE):
            Publication date as a normalized string. Format varies by
            source so stored as string rather than a date type.

        journal (str, NULLABLE):
            Journal or conference name. Null for preprints.

        doi (str, NULLABLE):
            DOI if known. Null for pure arXiv preprints that have
            not been published yet.

        arxiv_id (str, NULLABLE):
            arXiv ID if known. Null for DOI-only lookups.

        arxiv_categories (JSON, NOT NULL):
            List of arXiv subject categories. Empty list for DOI
            lookups. Used to verify heliophysics relevance.

        citation_count (int, NULLABLE):
            From Semantic Scholar. Null if not available or if the
            paper is too new to have citations indexed.

        source (str, NOT NULL):
            Which external API provided the primary data. Either
            'crossref' or 'arxiv'.

        fetched_at (datetime, NOT NULL):
            When this record was first retrieved. Set once and never
            updated — use this to track data freshness.

        created_at (datetime, NOT NULL):
            When this row was inserted into Postgres. Set by the
            database server via server_default.

    Indexes:
        ix_papers_identifier_type:
            Supports filtering papers by source type (doi vs arxiv).

        ix_papers_source:
            Supports filtering by which API provided the data.

        ix_papers_fetched_at:
            Supports time-based queries like 'papers fetched this week'
            and TTL-based cleanup jobs.
    """
    op.create_table(
        "papers",
        sa.Column("identifier", sa.String(), primary_key=True),
        sa.Column("identifier_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("authors", sa.JSON(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("published_date", sa.String(), nullable=True),
        sa.Column("journal", sa.String(), nullable=True),
        sa.Column("doi", sa.String(), nullable=True),
        sa.Column("arxiv_id", sa.String(), nullable=True),
        sa.Column("arxiv_categories", sa.JSON(), nullable=False),
        sa.Column("citation_count", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_papers_identifier_type", "papers", ["identifier_type"])
    op.create_index("ix_papers_source", "papers", ["source"])
    op.create_index("ix_papers_fetched_at", "papers", ["fetched_at"])


def downgrade():
    """Drop the papers table and all associated indexes.

    Drops indexes before the table explicitly for clarity even though
    dropping the table would implicitly drop them too.

    Warning:
        This is destructive in any environment with real data.
        Never run against production without a backup.
    """
    op.drop_index("ix_papers_fetched_at", "papers")
    op.drop_index("ix_papers_source", "papers")
    op.drop_index("ix_papers_identifier_type", "papers")
    op.drop_table("papers")
