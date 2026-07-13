"""Tests for the two-layer test-database safety guard
(test_support/db_safety.py). This is the first test file allowed to run
in this effort — everything else waits until this passes.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.config import Settings
from test_support.db_safety import assert_safe_test_connection, assert_safe_test_database_url

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- URL-level guard (re-asserted via normal pytest collection) -------------


def test_url_guard_rejects_the_development_database():
    with pytest.raises(RuntimeError, match="msl_insights"):
        assert_safe_test_database_url("postgresql+psycopg://msl:msl@localhost:5432/msl_insights")


def test_url_guard_rejects_a_non_approved_host():
    with pytest.raises(RuntimeError, match="host"):
        assert_safe_test_database_url(
            "postgresql+psycopg://msl:msl@example.com:5432/msl_insights_test"
        )


def test_url_guard_rejects_a_non_approved_port():
    with pytest.raises(RuntimeError, match="port"):
        assert_safe_test_database_url(
            "postgresql+psycopg://msl:msl@localhost:9999/msl_insights_test"
        )


def test_url_guard_accepts_the_approved_test_database():
    assert_safe_test_database_url("postgresql+psycopg://msl:msl@localhost:5432/msl_insights_test")


# --- Live connection-level guard --------------------------------------------


def test_connection_guard_refuses_the_development_database():
    """Opens a real, non-destructive, SELECT-permitting connection to the
    development database and proves the guard refuses it. This test never
    calls create_all/drop_all against that connection under any
    circumstance, including in exception-handling paths.
    """
    dev_url = Settings.model_fields["database_url"].default
    dev_engine = create_engine(dev_url)
    try:
        with dev_engine.connect() as connection:
            with pytest.raises(RuntimeError, match="development database"):
                assert_safe_test_connection(connection)
    finally:
        dev_engine.dispose()


def test_connection_guard_accepts_the_test_database(db_session):
    # db_session's underlying engine is already the verified test database
    # by the time any fixture runs — this just re-confirms the live check
    # itself passes against a real test-database connection.
    connection = db_session.connection()
    assert_safe_test_connection(connection)


# --- End-to-end regression test for the actual suspected incident ----------


def test_conftest_overrides_an_ambient_dev_database_url():
    """The regression test for the exact failure mode suspected to have
    caused the original incident: an ambient DATABASE_URL already pointing
    at the development database must NOT survive into the test session.

    conftest.py's module-level guard logic only runs once per process, so
    this must run in a fresh subprocess with DATABASE_URL deliberately
    pre-set to a dev-shaped URL (sourced from Settings' own class default,
    never re-typed as a new literal) before that subprocess's conftest.py
    ever loads.
    """
    import os

    dev_url = Settings.model_fields["database_url"].default
    env = {**os.environ, "DATABASE_URL": dev_url, "SKIP_TEST_DB_TEARDOWN": "1"}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_conftest_db_resolution_probe.py",
            "-q",
            "-s",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"Probe subprocess failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "RESOLVED_DATABASE=msl_insights_test" in result.stdout, (
        f"Probe did not resolve to the test database.\nstdout:\n{result.stdout}"
    )
