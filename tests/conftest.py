import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("MEDIA_APP_INTERNAL_SECRET", "test-internal-secret")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://msl:msl@localhost:5432/msl_insights_test"
)

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.project import Project


@pytest.fixture(scope="session", autouse=True)
def _database_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _clean_projects_table():
    yield
    with SessionLocal() as session:
        session.query(Project).delete()
        session.commit()


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path):
    settings = get_settings()
    original = settings.upload_root_dir
    settings.upload_root_dir = str(tmp_path / "uploads")
    yield
    settings.upload_root_dir = original


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def report_db():
    """A fresh session, separate from `db_session`.

    `build_project_report_data`/`build_comparison_report_data`
    (app/services/report_data.py) must issue `SET TRANSACTION ISOLATION
    LEVEL ...` as the *first* statement on their session (a hard Postgres
    requirement) — exactly like the real `GET /report.*` routes, which each
    get a brand-new per-request session from FastAPI's `get_db`. The shared
    `db_session` fixture already has prior statements on it by the time a
    test's `project_factory`/`article_factory` calls finish, so it cannot
    be reused for report-data calls under test.
    """
    with SessionLocal() as session:
        yield session


@pytest.fixture
def authenticated_client(client):
    response = client.post(
        "/login", data={"password": "test-password"}, follow_redirects=False
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def standard_workbook_path(tmp_path):
    from tests.fixtures.workbook_builder import build_standard_workbook

    return build_standard_workbook(tmp_path / "Auchan Q2 2026.xlsx")


@pytest.fixture
def penny_workbook_path(tmp_path):
    from tests.fixtures.workbook_builder import build_penny_workbook

    return build_penny_workbook(tmp_path / "Penny - Rewe Q2 2026.xlsx")


@pytest.fixture
def internal_headers():
    return {"x-internal-secret": "test-internal-secret"}


@pytest.fixture
def project_factory(db_session):
    from app.schemas.project import ProjectCreate
    from app.services.projects import create_project

    def _create(name="Test Project", quarter="2026-Q2"):
        return create_project(db_session, ProjectCreate(name=name, quarter=quarter))

    return _create


@pytest.fixture
def article_factory(db_session):
    import uuid
    from datetime import date

    from app.models.article import Article, ImportStatus
    from app.models.uploaded_file import UploadedFile, UploadedFileStatus

    def _create(project, count=1, retailer="Auchan", import_status=ImportStatus.VALID, **overrides):
        uploaded_file = UploadedFile(
            project_id=project.id,
            original_filename="fixture.xlsx",
            stored_filename=f"{uuid.uuid4().hex}.xlsx",
            stored_path=f"/tmp/{uuid.uuid4().hex}.xlsx",
            status=UploadedFileStatus.COMPLETED,
        )
        db_session.add(uploaded_file)
        db_session.flush()

        articles = []
        for i in range(count):
            defaults = dict(
                id=uuid.uuid4(),
                project_id=project.id,
                uploaded_file_id=uploaded_file.id,
                original_row_number=i + 1,
                retailer=retailer,
                title=f"Title {uuid.uuid4().hex[:8]}",
                source=f"Source {uuid.uuid4().hex[:8]}",
                subject="Subject",
                medium="online",
                publication_date=date(2026, 5, 1),
                audience=1000.0,
                sentiment_original="pozitiv",
                importance_original="mare",
                fingerprint=uuid.uuid4().hex,
                is_duplicate=False,
                import_status=import_status,
                raw_json={},
            )
            defaults.update(overrides)
            article = Article(**defaults)
            db_session.add(article)
            articles.append(article)

        db_session.commit()
        for article in articles:
            db_session.refresh(article)
        return articles

    return _create


@pytest.fixture
def classification_factory(db_session):
    import uuid

    from app.models.classification import Classification

    def _create(article, **overrides):
        defaults = dict(
            id=uuid.uuid4(),
            article_id=article.id,
            project_id=article.project_id,
            primary_topic="store_expansion",
            secondary_topic=None,
            communication_category="corporate",
            sentiment="positive",
            brand_role="primary_focus",
            story_key=None,
            confidence=0.9,
            model="deepseek-chat",
            prompt_version="retail-deepseek-v2",
        )
        defaults.update(overrides)
        classification = Classification(**defaults)
        db_session.add(classification)
        db_session.commit()
        db_session.refresh(classification)
        return classification

    return _create
