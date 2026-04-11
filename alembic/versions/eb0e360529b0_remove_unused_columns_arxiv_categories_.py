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
    pass  # columns already removed manually from database

def downgrade():
    pass