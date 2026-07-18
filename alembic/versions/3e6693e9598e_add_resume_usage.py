"""add resume usage

Revision ID: 3e6693e9598e
Revises: 5f210a6b2793
Create Date: 2026-07-18 16:49:59.614871

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3e6693e9598e'
down_revision: Union[str, Sequence[str], None] = '5f210a6b2793'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'resume_usage',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('vacancy_match_id', sa.Integer(), nullable=True),
        sa.Column(
            'generated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['vacancy_match_id'], ['vacancy_matches.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_resume_usage_generated_at'),
        'resume_usage',
        ['generated_at'],
        unique=False,
    )
    op.create_index(
        op.f('ix_resume_usage_user_id'),
        'resume_usage',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_resume_usage_user_id'), table_name='resume_usage'
    )
    op.drop_index(
        op.f('ix_resume_usage_generated_at'), table_name='resume_usage'
    )
    op.drop_table('resume_usage')