from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.database import Base, engine
from app.models.project import Project


def test_alembic_upgrade_head_creates_expected_schema():
    Base.metadata.drop_all(bind=engine)

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")

    inspector = inspect(engine)
    assert "projects" in inspector.get_table_names()

    columns = {col["name"] for col in inspector.get_columns("projects")}
    expected_columns = {
        "id",
        "name",
        "quarter",
        "description",
        "status",
        "total_files",
        "total_rows",
        "valid_rows",
        "invalid_rows",
        "duplicate_rows",
        "classified_rows",
        "analysis_status",
        "created_at",
        "updated_at",
    }
    assert expected_columns.issubset(columns)

    command.downgrade(alembic_cfg, "base")
    inspector = inspect(engine)
    assert "projects" not in inspector.get_table_names()

    # Restore schema for any tests that run after this module.
    Base.metadata.create_all(bind=engine)
    assert Project.__tablename__ in inspect(engine).get_table_names()
