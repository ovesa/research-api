"""merge migration heads

Revision ID: e4729ca00e20
Revises: 78856ed76d93, eb0e360529b0
Create Date: 2026-04-11 14:21:49.751266

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4729ca00e20'
down_revision: Union[str, Sequence[str], None] = ('78856ed76d93', 'eb0e360529b0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
