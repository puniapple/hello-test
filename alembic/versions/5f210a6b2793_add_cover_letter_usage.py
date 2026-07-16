"""add cover letter usage

Revision ID: 5f210a6b2793
Revises: 39dcd2188b85
Create Date: 2026-07-11 12:18:58.893864

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5f210a6b2793'
down_revision: Union[str, Sequence[str], None] = '39dcd2188b85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'cover_letter_usage',
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
        op.f('ix_cover_letter_usage_generated_at'),
        'cover_letter_usage',
        ['generated_at'],
        unique=False,
    )
    op.create_index(
        op.f('ix_cover_letter_usage_user_id'),
        'cover_letter_usage',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_cover_letter_usage_user_id'), table_name='cover_letter_usage'
    )
    op.drop_index(
        op.f('ix_cover_letter_usage_generated_at'), table_name='cover_letter_usage'
    )
    op.drop_table('cover_letter_usage')