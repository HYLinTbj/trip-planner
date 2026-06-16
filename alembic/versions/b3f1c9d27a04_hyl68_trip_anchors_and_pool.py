"""HYL-68 retire base: trip_day_anchors + trip_pois, trips.mode, nullable base

Revision ID: b3f1c9d27a04
Revises: d2e7a1f3c9b4
Create Date: 2026-06-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f1c9d27a04'
down_revision: Union[str, Sequence[str], None] = 'd2e7a1f3c9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'trip_day_anchors',
        sa.Column('trip_id', sa.Integer(), nullable=False),
        sa.Column('day_index', sa.Integer(), nullable=False),
        sa.Column('start_lat', sa.Float(), nullable=False),
        sa.Column('start_lon', sa.Float(), nullable=False),
        sa.Column('start_name', sa.String(), nullable=True),
        sa.Column('end_lat', sa.Float(), nullable=False),
        sa.Column('end_lon', sa.Float(), nullable=False),
        sa.Column('end_name', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('trip_id', 'day_index'),
    )
    op.create_table(
        'trip_pois',
        sa.Column('trip_id', sa.Integer(), nullable=False),
        sa.Column('city_slug', sa.String(), nullable=False),
        sa.Column('poi_id', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('trip_id', 'city_slug', 'poi_id'),
    )
    # Route trips carry no single base; mode records the trip shape (existing rows -> "base").
    op.add_column('trips', sa.Column('mode', sa.String(), nullable=False, server_default='base'))
    op.alter_column('trips', 'base_lat', existing_type=sa.Float(), nullable=True)
    op.alter_column('trips', 'base_lon', existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('trips', 'base_lon', existing_type=sa.Float(), nullable=False)
    op.alter_column('trips', 'base_lat', existing_type=sa.Float(), nullable=False)
    op.drop_column('trips', 'mode')
    op.drop_table('trip_pois')
    op.drop_table('trip_day_anchors')
