"""HYL-72 adjustable buffers: trips.travel_buffer_pct/_min + stop_buffer_min

Revision ID: e5b1d9c3a2f7
Revises: c7e3a9b2f4d1
Create Date: 2026-06-17 00:00:00.000000

Three integer contingency knobs persisted on a trip so re-optimize honors them. All default
to 0 (no-op) with a server_default, so existing rows migrate cleanly:
  - travel_buffer_pct : pad every leg by this percent
  - travel_buffer_min : pad every leg by this many flat minutes
  - stop_buffer_min   : flat per-stop cushion (reserved after each visit)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b1d9c3a2f7'
down_revision: Union[str, Sequence[str], None] = 'c7e3a9b2f4d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('trips', sa.Column('travel_buffer_pct', sa.Integer(),
                                     nullable=False, server_default='0'))
    op.add_column('trips', sa.Column('travel_buffer_min', sa.Integer(),
                                     nullable=False, server_default='0'))
    op.add_column('trips', sa.Column('stop_buffer_min', sa.Integer(),
                                     nullable=False, server_default='0'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('trips', 'stop_buffer_min')
    op.drop_column('trips', 'travel_buffer_min')
    op.drop_column('trips', 'travel_buffer_pct')
