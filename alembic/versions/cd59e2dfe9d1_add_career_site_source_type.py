"""add career_site source type

Revision ID: cd59e2dfe9d1
Revises: 5e2eee7491b5
Create Date: 2026-06-15 18:15:18.133132

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd59e2dfe9d1'
down_revision: Union[str, Sequence[str], None] = '5e2eee7491b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'career_site'")


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значений из enum.
    # Откат был бы пересозданием типа — для MVP оставляем no-op.
    pass

