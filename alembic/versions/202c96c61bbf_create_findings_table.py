"""create_findings_table

Revision ID: 202c96c61bbf
Revises: 0a08b190960b
Create Date: 2026-09-13 05:07:47.749169

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '202c96c61bbf'
down_revision: Union[str, Sequence[str], None] = '0a08b190960b'
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
        "findings",
        sa.Column("finding_id", sa.UUID(), nullable=False),
        sa.Column("investigation_run_id", sa.UUID(), nullable=False),
        sa.Column("observed_fact", sa.Text(), nullable=False),
        sa.Column("derived_signal", sa.Text(), nullable=True),
        sa.Column("interpretation", sa.Text(), nullable=True),
        sa.Column("evidence_ids", ARRAY(sa.UUID()), nullable=False),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"], ["investigation_runs.investigation_run_id"]
        ),
        sa.PrimaryKeyConstraint("finding_id"),
        sa.CheckConstraint(
            "confidence IN ('LOW', 'MEDIUM', 'HIGH')",
            name="chk_findings_confidence",
        ),
    )

    grant_script = (
        "DO $$\n"
        "BEGIN\n"
        "    EXECUTE format('GRANT SELECT, INSERT ON TABLE %I TO %I', "
        f"'findings', '{app_role}');\n"
        "    EXECUTE format('REVOKE UPDATE, DELETE ON TABLE %I FROM %I', "
        f"'findings', '{app_role}');\n"
        "END $$;\n"
    )
    op.execute(grant_script)


def downgrade() -> None:
    op.drop_table("findings")
