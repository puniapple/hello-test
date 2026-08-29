"""add shutdown_notified_at

Revision ID: 1ecee958f242
Revises: e6801ac00819
Create Date: 2026-08-29 15:02:46.705170

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1ecee958f242'
down_revision: Union[str, Sequence[str], None] = 'e6801ac00819'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column(
            'shutdown_notified_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'shutdown_notified_at')