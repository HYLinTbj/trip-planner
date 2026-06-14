"""add trips + trip_stops, drop plans

Trips become first-class: normalized stops (sequence/time as real columns) with the
raw solver result retained as JSON. Replaces the phase-1 `plans` scaffolding (opaque
JSON blobs, never used by any client — its 1 row was a smoke-test artifact, dropped
without data migration).

Revision ID: c4d81f2ab5e7
Revises: 1b97ca67bc0b
Create Date: 2026-06-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c4d81f2ab5e7'
down_revision: Union[str, Sequence[str], None] = '1b97ca67bc0b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('trips',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('city_slug', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='draft'),
        sa.Column('notes', sa.String(), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('num_days', sa.Integer(), nullable=False),
        sa.Column('day_start_min', sa.Integer(), nullable=False),
        sa.Column('day_end_min', sa.Integer(), nullable=False),
        sa.Column('profile', sa.String(), nullable=False),
        sa.Column('balance', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('base_lat', sa.Float(), nullable=False),
        sa.Column('base_lon', sa.Float(), nullable=False),
        sa.Column('locks', sa.JSON(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('total_travel_min', sa.Integer(), nullable=True),
        sa.Column('feasible', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['city_slug'], ['cities.slug'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_trips_city_slug'), 'trips', ['city_slug'], unique=False)
    op.create_table('trip_stops',
        sa.Column('trip_id', sa.Integer(), nullable=False),
        sa.Column('day_index', sa.Integer(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('poi_id', sa.String(), nullable=True),   # soft reference — no FK on purpose
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('lat', sa.Float(), nullable=False),
        sa.Column('lon', sa.Float(), nullable=False),
        sa.Column('dwell_min', sa.Integer(), nullable=False),
        sa.Column('arrival_min', sa.Integer(), nullable=False),
        sa.Column('departure_min', sa.Integer(), nullable=False),
        sa.Column('travel_in_min', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['trip_id'], ['trips.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('trip_id', 'day_index', 'seq'),
    )
    op.drop_index(op.f('ix_plans_city_slug'), table_name='plans')
    op.drop_table('plans')


def downgrade() -> None:
    op.create_table('plans',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('city_slug', sa.String(), nullable=False),
        sa.Column('label', sa.String(), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('locks', sa.JSON(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['city_slug'], ['cities.slug'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_plans_city_slug'), 'plans', ['city_slug'], unique=False)
    op.drop_table('trip_stops')
    op.drop_index(op.f('ix_trips_city_slug'), table_name='trips')
    op.drop_table('trips')
