"""add score breakdowns

Revision ID: b953db0911e8
Revises: a1b2c3d4e5f6
Create Date: 2026-07-29 07:40:17.641345

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b953db0911e8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Новая таблица score_breakdowns
    op.create_table(
        'score_breakdowns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('match_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('breakdown_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('model_used', sa.String(length=64), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['match_id'], ['vacancy_matches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('match_id'),
    )
    op.create_index(
        op.f('ix_score_breakdowns_match_id'),
        'score_breakdowns',
        ['match_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_score_breakdowns_user_id'),
        'score_breakdowns',
        ['user_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_score_breakdowns_created_at'),
        'score_breakdowns',
        ['created_at'],
        unique=False,
    )

    # 2. Поле free_breakdown_used_at в users
    op.add_column(
        'users',
        sa.Column('free_breakdown_used_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'free_breakdown_used_at')
    op.drop_index(op.f('ix_score_breakdowns_created_at'), table_name='score_breakdowns')
    op.drop_index(op.f('ix_score_breakdowns_user_id'), table_name='score_breakdowns')
    op.drop_index(op.f('ix_score_breakdowns_match_id'), table_name='score_breakdowns')
    op.drop_table('score_breakdowns')