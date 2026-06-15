"""add cities.user_created

Revision ID: d2e7a1f3c9b4
Revises: c4d81f2ab5e7
Create Date: 2026-06-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e7a1f3c9b4'
down_revision: Union[str, Sequence[str], None] = 'c4d81f2ab5e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'cities',
        sa.Column('user_created', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('cities', 'user_created')
