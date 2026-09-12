"""Tests for evidence module."""

import uuid
from datetime import datetime, timezone

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text

from alembic import command
from meridian.evidence.evidence import record_evidence
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


def _seed_transaction(
    superuser_engine: Engine,
    source_account_id: uuid.UUID | None,
    amount: float,
    days_ago: int,
) -> uuid.UUID:
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
                "VALUES (:tid, :src, :amount, 'INR', :occ, :now)"
            ),
            {
                "tid": tid,
                "src": source_account_id,
                "amount": amount,
                "occ": occurred_at,
                "now": datetime.now(timezone.utc),
            },
        )
    return tid


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

    # downgrade to the revision immediately before evidence
    command.downgrade(alembic_cfg, "644b6b3e6eb8")

    with superuser_engine.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(text("SELECT * FROM evidence"))

    # upgrade back to head
    command.upgrade(alembic_cfg, "head")

    with superuser_engine.connect() as conn:
        res = conn.execute(text("SELECT count(*) FROM evidence")).scalar()
        assert res == 0


def test_fk_integrity(app_role_engine: Engine, superuser_engine: Engine) -> None:
    """Inserting with non-existent investigation_run_id or agent_run_id fails."""

    with app_role_engine.connect() as conn:
        # 1. Nonexistent investigation_run_id, nonexistent agent_run_id
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                text(
                    "INSERT INTO evidence ("
                    "evidence_id, investigation_run_id, evidence_type, "
                    "reference_table, reference_id, "
                    "produced_by_agent_run_id, created_at) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), 'transaction', "
                    "'transactions', gen_random_uuid(), gen_random_uuid(), NOW())"
                )
            )
            conn.commit()
        assert "foreign key constraint" in str(excinfo.value).lower()
        conn.rollback()

    cid, tid = setup_customer_and_transaction(superuser_engine)
    try:
        alert_id = setup_alert(superuser_engine, cid, tid)
        case_id = setup_case(superuser_engine, alert_id)
        inv_run = create_investigation_run(app_role_engine, case_id)

        # Seed an agent run to have a valid produced_by_agent_run_id
        ar_id = uuid.uuid4()
        with app_role_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_runs ("
                    "agent_run_id, investigation_run_id, agent_name, "
                    "tool_calls, status, started_at) "
                    "VALUES (:ar_id, :inv_id, 'test_agent', "
                    "'{}'::jsonb, 'SUCCESS', :now)"
                ),
                {
                    "ar_id": ar_id,
                    "inv_id": inv_run.investigation_run_id,
                    "now": datetime.now(timezone.utc),
                },
            )

        with app_role_engine.connect() as conn:
            # Valid inv_run_id, invalid agent_run_id
            with pytest.raises(Exception) as excinfo:
                conn.execute(
                    text(
                        "INSERT INTO evidence ("
                        "evidence_id, investigation_run_id, evidence_type, "
                        "reference_table, reference_id, "
                        "produced_by_agent_run_id, created_at) "
                        "VALUES (gen_random_uuid(), :inv_id, 'transaction', "
                        "'transactions', gen_random_uuid(), gen_random_uuid(), NOW())"
                    ),
                    {"inv_id": inv_run.investigation_run_id},
                )
                conn.commit()
            assert "foreign key constraint" in str(excinfo.value).lower()
            conn.rollback()

            # Invalid inv_run_id, valid agent_run_id
            with pytest.raises(Exception) as excinfo:
                conn.execute(
                    text(
                        "INSERT INTO evidence ("
                        "evidence_id, investigation_run_id, evidence_type, "
                        "reference_table, reference_id, "
                        "produced_by_agent_run_id, created_at) "
                        "VALUES (gen_random_uuid(), gen_random_uuid(), 'transaction', "
                        "'transactions', gen_random_uuid(), :ar_id, NOW())"
                    ),
                    {"ar_id": ar_id},
                )
                conn.commit()
            assert "foreign key constraint" in str(excinfo.value).lower()
            conn.rollback()

    finally:
        _cleanup_seeded_data(superuser_engine, cid)


def test_app_role_privileges(app_role_engine: Engine, superuser_engine: Engine) -> None:
    """meridian_app can SELECT/INSERT but not UPDATE/DELETE on evidence."""
    cid, tid = setup_customer_and_transaction(superuser_engine)
    try:
        alert_id = setup_alert(superuser_engine, cid, tid)
        case_id = setup_case(superuser_engine, alert_id)
        inv_run = create_investigation_run(app_role_engine, case_id)

        ar_id = uuid.uuid4()
        with app_role_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_runs ("
                    "agent_run_id, investigation_run_id, agent_name, "
                    "tool_calls, status, started_at) "
                    "VALUES (:ar_id, :inv_id, 'test_agent', "
                    "'{}'::jsonb, 'SUCCESS', :now)"
                ),
                {
                    "ar_id": ar_id,
                    "inv_id": inv_run.investigation_run_id,
                    "now": datetime.now(timezone.utc),
                },
            )

        ev_id = uuid.uuid4()

        with app_role_engine.connect() as conn:
            # INSERT should succeed
            conn.execute(
                text(
                    "INSERT INTO evidence ("
                    "evidence_id, investigation_run_id, evidence_type, "
                    "reference_table, reference_id, "
                    "produced_by_agent_run_id, created_at) "
                    "VALUES (:ev_id, :inv_id, 'transaction', "
                    "'transactions', :tid, :ar_id, :now)"
                ),
                {
                    "ev_id": ev_id,
                    "inv_id": inv_run.investigation_run_id,
                    "tid": tid,
                    "ar_id": ar_id,
                    "now": datetime.now(timezone.utc),
                },
            )
            conn.commit()

            # SELECT should succeed
            count = conn.execute(
                text("SELECT count(*) FROM evidence WHERE evidence_id = :ev_id"),
                {"ev_id": ev_id},
            ).scalar()
            assert count == 1

            # UPDATE should fail
            with pytest.raises(Exception) as excinfo:
                conn.execute(
                    text(
                        "UPDATE evidence SET evidence_type = 'beneficiary' "
                        "WHERE evidence_id = :ev_id"
                    ),
                    {"ev_id": ev_id},
                )
                conn.commit()
            assert "permission denied" in str(excinfo.value).lower()
            conn.rollback()

            # DELETE should fail
            with pytest.raises(Exception) as excinfo:
                conn.execute(
                    text("DELETE FROM evidence WHERE evidence_id = :ev_id"),
                    {"ev_id": ev_id},
                )
                conn.commit()
            assert "permission denied" in str(excinfo.value).lower()
            conn.rollback()

    finally:
        _cleanup_seeded_data(superuser_engine, cid)


def test_real_proof_case(app_role_engine: Engine, superuser_engine: Engine) -> None:
    """Proof case linking real agent run to evidence row."""
    cid, tid = setup_customer_and_transaction(
        superuser_engine, amount=450.0, days_ago=0
    )
    try:
        with superuser_engine.connect() as conn:
            aid = conn.execute(
                text("SELECT account_id FROM accounts WHERE customer_id = :cid"),
                {"cid": cid},
            ).scalar()
        # Seed history
        _seed_transaction(superuser_engine, aid, 100.0, 10)
        _seed_transaction(superuser_engine, aid, 200.0, 5)

        alert_id = setup_alert(superuser_engine, cid, tid)
        case_id = setup_case(superuser_engine, alert_id)

        # 1. Call create_investigation_run()
        inv_run = create_investigation_run(app_role_engine, case_id)

        # 2. Call run_transaction_agent()
        _ = run_transaction_agent(
            app_role_engine,
            inv_run.investigation_run_id,
            tid,
        )

        # 3. Obtain the real agent_run_id from agent_runs
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
        evidence_result = record_evidence(
            app_role_engine,
            investigation_run_id=inv_run.investigation_run_id,
            evidence_type="transaction",
            reference_table="transactions",
            reference_id=tid,
            produced_by_agent_run_id=ar_id,
        )

        # 5. Assert exactly one evidence row exists
        with superuser_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT evidence_id, investigation_run_id, evidence_type, "
                    "reference_table, reference_id, produced_by_agent_run_id "
                    "FROM evidence WHERE investigation_run_id = :inv_id"
                ),
                {"inv_id": inv_run.investigation_run_id},
            ).fetchall()

        assert len(rows) == 1

        # 6. Assert all evidence fields are correct
        db_ev = rows[0]
        assert db_ev[0] == evidence_result.evidence_id
        assert db_ev[1] == evidence_result.investigation_run_id
        assert db_ev[2] == "transaction"
        assert db_ev[3] == "transactions"
        assert db_ev[4] == tid
        assert db_ev[5] == ar_id

        # 7. Assert the evidence row's investigation_run_id equals
        # the investigation run associated with that same agent_run_id
        with superuser_engine.connect() as conn:
            ar_inv_id = conn.execute(
                text(
                    "SELECT investigation_run_id FROM agent_runs "
                    "WHERE agent_run_id = :ar_id"
                ),
                {"ar_id": ar_id},
            ).scalar()

        assert ar_inv_id == evidence_result.investigation_run_id
        assert db_ev[1] == ar_inv_id

    finally:
        _cleanup_seeded_data(superuser_engine, cid)
