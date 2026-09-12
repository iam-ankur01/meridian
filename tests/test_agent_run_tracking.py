"""Tests for agent_run_tracking."""
# ruff: noqa: E501

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text

from alembic import command
from meridian.agents.transaction.amount_deviation import (
    AmountDeviationComputed,
    compute_amount_deviation,
)
from meridian.orchestration.agent_run_tracking import record_agent_run


def _seed_customer(superuser_engine: Engine) -> uuid.UUID:
    cid = uuid.uuid4()
    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO customers (customer_id, full_name, is_synthetic, created_at) "
                "VALUES (:cid, 'Test Customer', true, :now)"
            ),
            {"cid": cid, "now": datetime.now(timezone.utc)},
        )
    return cid

def _seed_account(superuser_engine: Engine, customer_id: uuid.UUID) -> uuid.UUID:
    aid = uuid.uuid4()
    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (account_id, customer_id, status, created_at) "
                "VALUES (:aid, :cid, 'active', :now)"
            ),
            {"aid": aid, "cid": customer_id, "now": datetime.now(timezone.utc)},
        )
    return aid

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
                "INSERT INTO transactions (transaction_id, source_account_id, amount, currency, occurred_at, created_at) "
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

def _seed_alert_case_investigation(
    superuser_engine: Engine, customer_id: uuid.UUID, transaction_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    alert_id = uuid.uuid4()
    case_id = uuid.uuid4()
    inv_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO alerts (alert_id, customer_id, transaction_id, created_at) "
                "VALUES (:aid, :cid, :tid, :now)"
            ),
            {"aid": alert_id, "cid": customer_id, "tid": transaction_id, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO cases (case_id, alert_id, status, opened_at, created_at) "
                "VALUES (:case_id, :aid, 'OPEN', :now, :now)"
            ),
            {"case_id": case_id, "aid": alert_id, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO investigation_runs (investigation_run_id, case_id, status, started_at, created_at) "
                "VALUES (:inv_id, :case_id, 'IN_PROGRESS', :now, :now)"
            ),
            {"inv_id": inv_id, "case_id": case_id, "now": now},
        )
    return alert_id, case_id, inv_id

def _cleanup_seeded_data(superuser_engine: Engine, customer_id: uuid.UUID) -> None:
    with superuser_engine.begin() as conn:
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
        conn.execute(text("DELETE FROM accounts WHERE customer_id = :cid"), {"cid": customer_id})
        conn.execute(text("DELETE FROM customers WHERE customer_id = :cid"), {"cid": customer_id})


def test_migration_downgrade_upgrade(superuser_engine: Engine) -> None:
    """Test that migration downgrade drops the table and upgrade recreates it."""
    alembic_cfg = Config("alembic.ini")

    # downgrade to just before agent_runs
    command.downgrade(alembic_cfg, "c4250b2c09d2")

    with superuser_engine.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(text("SELECT * FROM agent_runs"))

    # upgrade 1 step
    command.upgrade(alembic_cfg, "head")

    with superuser_engine.connect() as conn:
        res = conn.execute(text("SELECT count(*) FROM agent_runs")).scalar()
        assert res == 0


def test_app_role_privileges(app_role_engine: Engine, superuser_engine: Engine) -> None:
    """meridian_app can SELECT/INSERT but not UPDATE/DELETE on agent_runs."""
    cid = _seed_customer(superuser_engine)
    try:
        aid = _seed_account(superuser_engine, cid)
        tid = _seed_transaction(superuser_engine, aid, 100.0, 0)
        _, _, inv_id = _seed_alert_case_investigation(superuser_engine, cid, tid)

        ar_id = uuid.uuid4()

        with app_role_engine.connect() as conn:
            # INSERT should succeed
            conn.execute(
                text(
                    "INSERT INTO agent_runs (agent_run_id, investigation_run_id, agent_name, tool_calls, status, started_at) "
                    "VALUES (:ar_id, :inv_id, 'test_agent', '{}'::jsonb, 'SUCCESS', :now)"
                ),
                {"ar_id": ar_id, "inv_id": inv_id, "now": datetime.now(timezone.utc)},
            )
            conn.commit()

            # SELECT should succeed
            count = conn.execute(text("SELECT count(*) FROM agent_runs WHERE agent_run_id = :ar_id"), {"ar_id": ar_id}).scalar()
            assert count == 1

            # UPDATE should fail
            with pytest.raises(Exception) as excinfo:
                conn.execute(text("UPDATE agent_runs SET status = 'FAILED' WHERE agent_run_id = :ar_id"), {"ar_id": ar_id})
                conn.commit()
            assert "permission denied" in str(excinfo.value).lower()
            conn.rollback()

            # DELETE should fail
            with pytest.raises(Exception) as excinfo:
                conn.execute(text("DELETE FROM agent_runs WHERE agent_run_id = :ar_id"), {"ar_id": ar_id})
                conn.commit()
            assert "permission denied" in str(excinfo.value).lower()
            conn.rollback()

    finally:
        _cleanup_seeded_data(superuser_engine, cid)


def test_fk_integrity(app_role_engine: Engine) -> None:
    """Inserting with non-existent investigation_run_id fails."""
    with app_role_engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                text(
                    "INSERT INTO agent_runs (agent_run_id, investigation_run_id, agent_name, tool_calls, status, started_at) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), 'test_agent', '{}'::jsonb, 'SUCCESS', NOW())"
                )
            )
            conn.commit()
        assert "foreign key constraint" in str(excinfo.value).lower()


def test_record_agent_run_success(app_role_engine: Engine, superuser_engine: Engine) -> None:
    """Successful invocation produces a SUCCESS row and returns the correct value."""
    cid = _seed_customer(superuser_engine)
    try:
        aid = _seed_account(superuser_engine, cid)
        # Create history and a current transaction
        _seed_transaction(superuser_engine, aid, 100.0, 10)
        _seed_transaction(superuser_engine, aid, 200.0, 5)
        tid = _seed_transaction(superuser_engine, aid, 450.0, 0) # alerted transaction

        _, _, inv_id = _seed_alert_case_investigation(superuser_engine, cid, tid)

        result = record_agent_run(
            app_role_engine,
            inv_id,
            "TransactionAgent",
            compute_amount_deviation,
            app_role_engine,  # pass engine as first positional arg for compute_amount_deviation
            tid,
        )

        assert isinstance(result, AmountDeviationComputed)
        assert result.deviation_multiple == 3.0  # 450 / 150 = 3

        # Verify db record
        with superuser_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT status, error, tool_calls FROM agent_runs WHERE investigation_run_id = :inv_id"),
                {"inv_id": inv_id},
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "SUCCESS"
            assert rows[0][1] is None
            assert rows[0][2] == {}

    finally:
        _cleanup_seeded_data(superuser_engine, cid)


def _dummy_agent_fail(*args: Any, **kwargs: Any) -> None:
    raise ValueError("deliberate test failure")


def test_record_agent_run_failure(app_role_engine: Engine, superuser_engine: Engine) -> None:
    """Failed invocation produces a FAILED row and re-raises exception."""
    cid = _seed_customer(superuser_engine)
    try:
        aid = _seed_account(superuser_engine, cid)
        tid = _seed_transaction(superuser_engine, aid, 100.0, 0)
        _, _, inv_id = _seed_alert_case_investigation(superuser_engine, cid, tid)

        with pytest.raises(ValueError, match="deliberate test failure"):
            record_agent_run(
                app_role_engine,
                inv_id,
                "TestAgent",
                _dummy_agent_fail,
            )

        # Verify db record
        with superuser_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT status, error, tool_calls FROM agent_runs WHERE investigation_run_id = :inv_id"),
                {"inv_id": inv_id},
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "FAILED"
            assert "deliberate test failure" in rows[0][1]
            assert rows[0][2] == {}

    finally:
        _cleanup_seeded_data(superuser_engine, cid)
