"""HYL-69 modifiable days: replace trips.day_start_min/day_end_min with day_windows JSON

Revision ID: c7e3a9b2f4d1
Revises: b3f1c9d27a04
Create Date: 2026-06-16 00:00:00.000000

Each day now carries its own time window, so the single (day_start_min, day_end_min)
scalar pair becomes a per-day `day_windows` list ([[start_min, end_min], …], one per day).

No backfill: the product is still experimental (no real trips to preserve), so this drops
the two scalar columns and adds a NOT NULL `day_windows`. Run against a fresh/empty `trips`
table — reset the dev DB (recreate the Postgres volume, or `downgrade base && upgrade head`)
rather than migrating data in place.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e3a9b2f4d1'
down_revision: Union[str, Sequence[str], None] = 'b3f1c9d27a04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('trips', sa.Column('day_windows', sa.JSON(), nullable=False))
    op.drop_column('trips', 'day_start_min')
    op.drop_column('trips', 'day_end_min')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('trips', sa.Column('day_end_min', sa.Integer(), nullable=False))
    op.add_column('trips', sa.Column('day_start_min', sa.Integer(), nullable=False))
    op.drop_column('trips', 'day_windows')
