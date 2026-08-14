"""add pending_paywall_notice

Revision ID: e6801ac00819
Revises: b953db0911e8
Create Date: 2026-08-14 19:14:14.958563

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e6801ac00819'
down_revision: Union[str, Sequence[str], None] = 'b953db0911e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column(
            'pending_paywall_notice',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'pending_paywall_notice')