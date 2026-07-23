"""add gate_notified_at to users

Revision ID: 5ce5bbbc7f32
Revises: 3e6693e9598e
Create Date: 2026-07-23 09:40:17.876010

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5ce5bbbc7f32'
down_revision: Union[str, Sequence[str], None] = '3e6693e9598e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column('gate_notified_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'gate_notified_at')