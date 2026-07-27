"""add clicked_at to vacancy_matches

Revision ID: a1b2c3d4e5f6
Revises: 3e6693e9598e
Create Date: 2026-07-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "a1b2c3d4e5f6"
down_revision = "5ce5bbbc7f32"  # проверь что это последняя миграция!
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vacancy_matches",
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_vacancy_matches_clicked_at",
        "vacancy_matches",
        ["clicked_at"],
        postgresql_where=sa.text("clicked_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_vacancy_matches_clicked_at", table_name="vacancy_matches")
    op.drop_column("vacancy_matches", "clicked_at")