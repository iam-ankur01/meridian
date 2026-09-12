"""create evidence table

Revision ID: 0a08b190960b
Revises: 644b6b3e6eb8
Create Date: 2026-09-13 04:19:25.284914

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0a08b190960b'
down_revision: Union[str, Sequence[str], None] = '644b6b3e6eb8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    app_role = os.environ.get("APP_DB_USER", "meridian_app")
    bind = op.get_bind()

    # Check role exists
    result = bind.execute(
        sa.text("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :role_name"),
        {"role_name": app_role},
    ).scalar()
    if not result:
        raise RuntimeError(
            f"Expected application role '{app_role}' does not exist; "
            "run the bootstrap init script before applying migrations"
        )

    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.UUID(), nullable=False),
        sa.Column("investigation_run_id", sa.UUID(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("reference_table", sa.Text(), nullable=False),
        sa.Column("reference_id", sa.UUID(), nullable=False),
        sa.Column("produced_by_agent_run_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"], ["investigation_runs.investigation_run_id"]
        ),
        sa.ForeignKeyConstraint(
            ["produced_by_agent_run_id"], ["agent_runs.agent_run_id"]
        ),
        sa.PrimaryKeyConstraint("evidence_id"),
    )

    grant_script = (
        "DO $$\n"
        "BEGIN\n"
        "    EXECUTE format('GRANT SELECT, INSERT ON TABLE %I TO %I', "
        f"'evidence', '{app_role}');\n"
        "    EXECUTE format('REVOKE UPDATE, DELETE ON TABLE %I FROM %I', "
        f"'evidence', '{app_role}');\n"
        "END $$;\n"
    )
    op.execute(grant_script)


def downgrade() -> None:
    op.drop_table("evidence")
