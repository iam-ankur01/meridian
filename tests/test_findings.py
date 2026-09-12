"""Tests for findings module."""

import uuid
from datetime import datetime, timezone

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text

from alembic import command
from meridian.evidence.evidence import record_evidence
from meridian.findings.findings import (
    evaluate_evidence_sufficiency,
    record_finding,
)
from meridian.orchestration.investigation_run import create_investigation_run
from meridian.orchestration.transaction_agent_dispatch import run_transaction_agent


def setup_customer_and_transaction(
    superuser_engine: Engine, amount: float = 100.0, days_ago: int = 0
) -> tuple[uuid.UUID, uuid.UUID]:
    cid = uuid.uuid4()
    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO customers "
                "(customer_id, full_name, is_synthetic, created_at) "
                "VALUES (:cid, 'Test Customer', true, :now)"
            ),
            {"cid": cid, "now": datetime.now(timezone.utc)},
        )

    aid = uuid.uuid4()
    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (account_id, customer_id, status, created_at) "
                "VALUES (:aid, :cid, 'active', :now)"
            ),
            {"aid": aid, "cid": cid, "now": datetime.now(timezone.utc)},
        )

    tid = uuid.uuid4()
    occurred_at = datetime.now(timezone.utc)
    if days_ago > 0:
        import datetime as dt

        occurred_at -= dt.timedelta(days=days_ago)

    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO transactions "
                "(transaction_id, source_account_id, amount, "
                "currency, occurred_at, created_at) "
                "VALUES (:tid, :aid, :amount, 'INR', :occ, :now)"
            ),
            {
                "tid": tid,
                "aid": aid,
                "amount": amount,
                "occ": occurred_at,
                "now": datetime.now(timezone.utc),
            },
        )
    return cid, tid


def setup_alert(
    superuser_engine: Engine, customer_id: uuid.UUID, transaction_id: uuid.UUID
) -> uuid.UUID:
    alert_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO alerts "
                "(alert_id, customer_id, transaction_id, created_at) "
                "VALUES (:aid, :cid, :tid, :now)"
            ),
            {"aid": alert_id, "cid": customer_id, "tid": transaction_id, "now": now},
        )
    return alert_id


def setup_case(
    superuser_engine: Engine, alert_id: uuid.UUID
) -> uuid.UUID:
    """Helper to setup a case for an alert."""
    case_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases "
                "(case_id, alert_id, status, created_at) "
                "VALUES (:cid, :aid, 'OPEN', :now)"
            ),
            {"cid": case_id, "aid": alert_id, "now": now},
        )
    return case_id


def _cleanup_seeded_data(superuser_engine: Engine, customer_id: uuid.UUID) -> None:
    with superuser_engine.begin() as conn:
        conn.execute(text("DELETE FROM findings"))
        conn.execute(text("DELETE FROM evidence"))
        conn.execute(text("DELETE FROM agent_runs"))
        conn.execute(text("DELETE FROM investigation_runs"))
        conn.execute(text("DELETE FROM cases"))
        conn.execute(text("DELETE FROM alerts"))
        conn.execute(
            text(
                "DELETE FROM transactions WHERE source_account_id IN "
                "(SELECT account_id FROM accounts WHERE customer_id = :cid)"
            ),
            {"cid": customer_id},
        )
        conn.execute(
            text("DELETE FROM accounts WHERE customer_id = :cid"), {"cid": customer_id}
        )
        conn.execute(
            text("DELETE FROM customers WHERE customer_id = :cid"), {"cid": customer_id}
        )


def test_migration_downgrade_upgrade(superuser_engine: Engine) -> None:
    """Test that migration downgrade drops the table and upgrade recreates it."""
    alembic_cfg = Config("alembic.ini")

    # The prior revision before findings is 0a08b190960b
    command.downgrade(alembic_cfg, "0a08b190960b")

    with superuser_engine.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(text("SELECT * FROM findings"))

    # upgrade to head
    command.upgrade(alembic_cfg, "head")

    with superuser_engine.connect() as conn:
        res = conn.execute(text("SELECT count(*) FROM findings")).scalar()
        assert res == 0


def test_fk_integrity(app_role_engine: Engine) -> None:
    """Attempt to insert a finding using a nonexistent investigation_run_id."""
    with app_role_engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                text(
                    "INSERT INTO findings ("
                    "finding_id, investigation_run_id, observed_fact, "
                    "derived_signal, interpretation, evidence_ids, "
                    "confidence, created_at) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), 'fact', "
                    "NULL, NULL, '{}', 'MEDIUM', NOW())"
                )
            )
            conn.commit()
        assert "foreign key constraint" in str(excinfo.value).lower()
        conn.rollback()


def test_confidence_check_constraint(
    app_role_engine: Engine, superuser_engine: Engine
) -> None:
    """Attempt to insert confidence = 'CRITICAL'."""
    cid, tid = setup_customer_and_transaction(superuser_engine)
    try:
        alert_id = setup_alert(superuser_engine, cid, tid)
        case_id = setup_case(superuser_engine, alert_id)
        inv_run = create_investigation_run(app_role_engine, case_id)

        with app_role_engine.connect() as conn:
            with pytest.raises(Exception) as excinfo:
                conn.execute(
                    text(
                        "INSERT INTO findings ("
                        "finding_id, investigation_run_id, observed_fact, "
                        "derived_signal, interpretation, evidence_ids, "
                        "confidence, created_at) "
                        "VALUES (gen_random_uuid(), :inv_id, 'fact', "
                        "NULL, NULL, '{}', 'CRITICAL', NOW())"
                    ),
                    {"inv_id": inv_run.investigation_run_id},
                )
                conn.commit()
            assert (
                "chk_findings_confidence" in str(excinfo.value).lower() or
                "check constraint" in str(excinfo.value).lower()
            )
            conn.rollback()
    finally:
        _cleanup_seeded_data(superuser_engine, cid)


def test_app_role_privileges(app_role_engine: Engine, superuser_engine: Engine) -> None:
    """meridian_app can SELECT/INSERT but not UPDATE/DELETE on findings."""
    cid, tid = setup_customer_and_transaction(superuser_engine)
    try:
        alert_id = setup_alert(superuser_engine, cid, tid)
        case_id = setup_case(superuser_engine, alert_id)
        inv_run = create_investigation_run(app_role_engine, case_id)

        f_id = uuid.uuid4()

        with app_role_engine.connect() as conn:
            # INSERT should succeed
            conn.execute(
                text(
                    "INSERT INTO findings ("
                    "finding_id, investigation_run_id, observed_fact, "
                    "derived_signal, interpretation, evidence_ids, "
                    "confidence, created_at) "
                    "VALUES (:f_id, :inv_id, 'test fact', "
                    "NULL, NULL, '{}', 'LOW', NOW())"
                ),
                {
                    "f_id": f_id,
                    "inv_id": inv_run.investigation_run_id,
                },
            )
            conn.commit()

            # SELECT should succeed
            count = conn.execute(
                text("SELECT count(*) FROM findings WHERE finding_id = :f_id"),
                {"f_id": f_id},
            ).scalar()
            assert count == 1

            # UPDATE should fail
            with pytest.raises(Exception) as excinfo:
                conn.execute(
                    text(
                        "UPDATE findings SET observed_fact = 'new fact' "
                        "WHERE finding_id = :f_id"
                    ),
                    {"f_id": f_id},
                )
                conn.commit()
            assert "permission denied" in str(excinfo.value).lower()
            conn.rollback()

            # DELETE should fail
            with pytest.raises(Exception) as excinfo:
                conn.execute(
                    text("DELETE FROM findings WHERE finding_id = :f_id"),
                    {"f_id": f_id},
                )
                conn.commit()
            assert "permission denied" in str(excinfo.value).lower()
            conn.rollback()

    finally:
        _cleanup_seeded_data(superuser_engine, cid)


def test_evaluate_evidence_sufficiency() -> None:
    """Pure evidence sufficiency test."""
    assert evaluate_evidence_sufficiency([]) is False
    assert evaluate_evidence_sufficiency([uuid.uuid4()]) is True
    assert evaluate_evidence_sufficiency([uuid.uuid4(), uuid.uuid4()]) is True


def test_record_finding_rejects_unresolvable_evidence_id(
    app_role_engine: Engine, superuser_engine: Engine
) -> None:
    """record_finding rejects if evidence_ids contains a nonexistent UUID."""
    cid, tid = setup_customer_and_transaction(superuser_engine)
    try:
        alert_id = setup_alert(superuser_engine, cid, tid)
        case_id = setup_case(superuser_engine, alert_id)
        inv_run = create_investigation_run(app_role_engine, case_id)

        random_evidence_id = uuid.uuid4()

        with pytest.raises(ValueError, match="do not exist in the evidence table"):
            record_finding(
                engine=app_role_engine,
                investigation_run_id=inv_run.investigation_run_id,
                observed_fact="Fact with invalid evidence",
                derived_signal=None,
                interpretation=None,
                evidence_ids=[random_evidence_id],
                confidence="LOW",
            )

        # Verify no finding row was inserted
        with superuser_engine.connect() as conn:
            res = conn.execute(
                text(
                    "SELECT count(*) FROM findings "
                    "WHERE investigation_run_id = :inv_id"
                ),
                {"inv_id": inv_run.investigation_run_id}
            ).scalar()
            assert res == 0

    finally:
        _cleanup_seeded_data(superuser_engine, cid)


def test_real_proof_case(app_role_engine: Engine, superuser_engine: Engine) -> None:
    """Proof case linking transaction agent dispatch to finding and sufficiency."""
    cid, tid = setup_customer_and_transaction(
        superuser_engine, amount=450.0, days_ago=0
    )
    try:
        # Seed history
        with superuser_engine.connect() as conn:
            aid = conn.execute(
                text("SELECT account_id FROM accounts WHERE customer_id = :cid"),
                {"cid": cid},
            ).scalar()

        # Generate some previous transactions to allow the deviation to be non-zero
        occurred_at_1 = datetime.now(timezone.utc)
        import datetime as dt
        occurred_at_1 -= dt.timedelta(days=10)

        with superuser_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(transaction_id, source_account_id, amount, "
                    "currency, occurred_at, created_at) "
                    "VALUES (:tid, :src, :amount, 'INR', :occ, :now)"
                ),
                {
                    "tid": uuid.uuid4(),
                    "src": aid,
                    "amount": 100.0,
                    "occ": occurred_at_1,
                    "now": datetime.now(timezone.utc),
                },
            )

        alert_id = setup_alert(superuser_engine, cid, tid)
        case_id = setup_case(superuser_engine, alert_id)

        # 1. Call create_investigation_run()
        inv_run = create_investigation_run(app_role_engine, case_id)

        # 2. Call run_transaction_agent()
        dispatch_res = run_transaction_agent(
            app_role_engine,
            inv_run.investigation_run_id,
            tid,
        )

        agent_result = dispatch_res.result

        # 3. Obtain the real agent_run_id
        with superuser_engine.connect() as conn:
            ar_id = conn.execute(
                text(
                    "SELECT agent_run_id FROM agent_runs "
                    "WHERE investigation_run_id = :inv_id "
                    "AND agent_name = 'TransactionAgent'"
                ),
                {"inv_id": inv_run.investigation_run_id},
            ).scalar()

        assert ar_id is not None

        # 4. Call record_evidence()
        evidence_record = record_evidence(
            app_role_engine,
            investigation_run_id=inv_run.investigation_run_id,
            evidence_type="transaction_amount_deviation",
            reference_table="transactions",
            reference_id=tid,
            produced_by_agent_run_id=ar_id,
        )

        # 5. Call record_finding()
        finding_record = record_finding(
            engine=app_role_engine,
            investigation_run_id=inv_run.investigation_run_id,
            observed_fact=(
                f"Transaction deviation multiple is "
                f"{agent_result.deviation_multiple:.2f}"
            ),
            derived_signal=f"Alerted amount: {agent_result.alerted_amount}",
            interpretation=(
                "The transaction deviates significantly from historical patterns."
            ),
            evidence_ids=[evidence_record.evidence_id],
            confidence="MEDIUM",
        )

        # 6. Verify finding fields
        assert finding_record.investigation_run_id == inv_run.investigation_run_id
        assert finding_record.observed_fact == (
            f"Transaction deviation multiple is "
            f"{agent_result.deviation_multiple:.2f}"
        )
        assert finding_record.derived_signal == (
            f"Alerted amount: {agent_result.alerted_amount}"
        )
        assert finding_record.evidence_ids == [evidence_record.evidence_id]
        assert finding_record.confidence == "MEDIUM"

        # 7. Verify evaluate_evidence_sufficiency
        assert evaluate_evidence_sufficiency(finding_record.evidence_ids) is True

    finally:
        _cleanup_seeded_data(superuser_engine, cid)
