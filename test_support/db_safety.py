"""Test-database safety guards.

Deliberately import-safe: only SQLAlchemy URL/text utilities and the
standard library. Never imports app.config, app.database, any model,
pytest, or conftest — so it can be imported and exercised (both from
conftest.py and from a standalone verification command) before any of
those exist. Lives outside tests/ to avoid any ambiguity about whether
tests/ is a real importable package.

Two independent layers:
- assert_safe_test_database_url: a string-level check on the URL a test
  run is about to use, before any app module (and therefore any engine)
  is even imported.
- assert_safe_test_connection: a live, read-only, SELECT-only check
  against an actual open connection, run immediately before both
  create_all and drop_all — defense in depth against URL/config/
  search-path surprises the string check can't see.

Both fail closed: any parse error, mismatch, or unexpected value raises,
never silently defaults to "allow."
"""

from sqlalchemy import text
from sqlalchemy.engine import make_url

APPROVED_TEST_DATABASE = "msl_insights_test"
APPROVED_HOSTS = {"localhost", "127.0.0.1"}
APPROVED_PORT = 5432
APPROVED_TEST_SCHEMA = "public"


def assert_safe_test_database_url(url: str) -> None:
    """Raises RuntimeError unless `url` is unambiguously the local test
    database. No development URL/password is embedded here — the exact
    name/host/port allow-list is sufficient on its own, and the live
    connection guard below is the real defense against configuration
    surprises this string check can't see.
    """
    parsed = make_url(url)
    if parsed.database != APPROVED_TEST_DATABASE:
        raise RuntimeError(
            f"Refusing: database {parsed.database!r} is not {APPROVED_TEST_DATABASE!r}."
        )
    if parsed.host not in APPROVED_HOSTS:
        raise RuntimeError(f"Refusing: host {parsed.host!r} is not an approved local host.")
    if parsed.port not in (None, APPROVED_PORT):
        raise RuntimeError(
            f"Refusing: port {parsed.port!r} is not the approved local Postgres port."
        )


def assert_safe_test_connection(connection) -> None:
    """Live-connection guard. Read-only: a single SELECT, no writes.
    Called immediately before both create_all and drop_all.
    """
    db_name, schema, search_path = connection.execute(
        text("SELECT current_database(), current_schema(), current_setting('search_path')")
    ).one()
    if db_name == "msl_insights":
        raise RuntimeError("Refusing: connection points at the development database.")
    if db_name != APPROVED_TEST_DATABASE:
        raise RuntimeError(
            f"Refusing: connected database is {db_name!r}, not {APPROVED_TEST_DATABASE!r}."
        )
    if schema != APPROVED_TEST_SCHEMA:
        raise RuntimeError(
            f"Refusing: connected schema is {schema!r}, not {APPROVED_TEST_SCHEMA!r}."
        )
    if APPROVED_TEST_SCHEMA not in (search_path or ""):
        raise RuntimeError(
            f"Refusing: search_path {search_path!r} does not include {APPROVED_TEST_SCHEMA!r}."
        )
