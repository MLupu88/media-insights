"""Tiny companion probe used only by the subprocess-based end-to-end
regression test in test_database_safety_guard.py. Not meant to be
meaningful on its own beyond proving which database the real fixture-
provided engine/session actually resolved to.
"""

from sqlalchemy import text


def test_resolved_database_is_the_test_database(db_session):
    result = db_session.execute(text("SELECT current_database()")).scalar_one()
    print(f"RESOLVED_DATABASE={result}")
    assert result == "msl_insights_test"
