"""add clan_membership_events

Revision ID: 8764c40cd0a9
Revises: 887dd3ec8d4a
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8764c40cd0a9'
down_revision: Union[str, None] = '887dd3ec8d4a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('clan_membership_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('server_num', sa.Integer(), nullable=False),
    sa.Column('clan_guid', sa.String(length=36), nullable=False),
    sa.Column('clan_name', sa.String(length=64), nullable=False),
    sa.Column('steam_id', sa.String(length=32), nullable=False),
    sa.Column('character_name', sa.String(length=64), nullable=False),
    sa.Column('event_type', sa.String(length=8), nullable=False),
    sa.Column('recorded_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_clan_membership_events_id'), 'clan_membership_events', ['id'], unique=False)
    op.create_index(op.f('ix_clan_membership_events_clan_guid'), 'clan_membership_events', ['clan_guid'], unique=False)
    op.create_index(op.f('ix_clan_membership_events_steam_id'), 'clan_membership_events', ['steam_id'], unique=False)
    op.create_index('ix_clan_membership_events_guid_recorded', 'clan_membership_events', ['clan_guid', 'recorded_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_clan_membership_events_guid_recorded', table_name='clan_membership_events')
    op.drop_index(op.f('ix_clan_membership_events_steam_id'), table_name='clan_membership_events')
    op.drop_index(op.f('ix_clan_membership_events_clan_guid'), table_name='clan_membership_events')
    op.drop_index(op.f('ix_clan_membership_events_id'), table_name='clan_membership_events')
    op.drop_table('clan_membership_events')
