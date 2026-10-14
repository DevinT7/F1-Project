"""ingest_jobs: params, result, max_attempts, run_after (background jobs, Phase 8)

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ingest_jobs", sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False))
    op.add_column("ingest_jobs", sa.Column("run_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ingest_jobs", sa.Column("params", postgresql.JSONB(astext_type=sa.Text()),
                                           server_default=sa.text("'{}'::jsonb"), nullable=False))
    op.add_column("ingest_jobs", sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("ingest_jobs", "result")
    op.drop_column("ingest_jobs", "params")
    op.drop_column("ingest_jobs", "run_after")
    op.drop_column("ingest_jobs", "max_attempts")
