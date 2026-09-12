"""Evidence recording module."""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Engine, text


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: uuid.UUID
    investigation_run_id: uuid.UUID
    evidence_type: str
    reference_table: str
    reference_id: uuid.UUID
    produced_by_agent_run_id: uuid.UUID
    created_at: datetime


def record_evidence(
    engine: Engine,
    investigation_run_id: uuid.UUID,
    evidence_type: str,
    reference_table: str,
    reference_id: uuid.UUID,
    produced_by_agent_run_id: uuid.UUID,
) -> EvidenceRecord:
    """Record a piece of evidence discovered by an agent.

    Args:
        engine: Application-role SQLAlchemy Engine.
        investigation_run_id: The UUID of the investigation run.
        evidence_type: Type of evidence (e.g., 'transaction', 'beneficiary').
        reference_table: The source table for this evidence.
        reference_id: The polymorphic UUID of the referenced record.
        produced_by_agent_run_id: The UUID of the agent run that produced this evidence.

    Returns:
        An EvidenceRecord object containing the persisted evidence details.
    """
    evidence_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO evidence (
                    evidence_id,
                    investigation_run_id,
                    evidence_type,
                    reference_table,
                    reference_id,
                    produced_by_agent_run_id,
                    created_at
                ) VALUES (
                    :evidence_id,
                    :investigation_run_id,
                    :evidence_type,
                    :reference_table,
                    :reference_id,
                    :produced_by_agent_run_id,
                    :now
                )
                """
            ),
            {
                "evidence_id": evidence_id,
                "investigation_run_id": investigation_run_id,
                "evidence_type": evidence_type,
                "reference_table": reference_table,
                "reference_id": reference_id,
                "produced_by_agent_run_id": produced_by_agent_run_id,
                "now": now,
            },
        )

    return EvidenceRecord(
        evidence_id=evidence_id,
        investigation_run_id=investigation_run_id,
        evidence_type=evidence_type,
        reference_table=reference_table,
        reference_id=reference_id,
        produced_by_agent_run_id=produced_by_agent_run_id,
        created_at=now,
    )
