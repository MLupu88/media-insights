"""Part 4: Delete Project -- full cascade deletion (database + upload
directory), authentication/authorization boundaries, and the
name-confirmation gate.
"""

import uuid
from pathlib import Path

from app.models.article import Article
from app.models.chat import ChatSession
from app.models.classification import Classification
from app.models.import_batch import ImportBatch
from app.models.narrative import NarrativeGeneration
from app.models.project import Project
from app.models.uploaded_file import UploadedFile
from app.services.analytics import AnalyticsFilters
from app.services.chat_service import find_or_create_project_session
from app.services.imports import start_import_batch
from app.services.narrative_service import create_project_generation

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _create_project(client, db_session, name="Delete Me", quarter="2026-Q2") -> Project:
    response = client.post(
        "/projects", data={"name": name, "quarter": quarter}, follow_redirects=False
    )
    assert response.status_code == 303
    return db_session.query(Project).filter_by(name=name).one()


def _fully_populate_project(db_session, project, article_factory, classification_factory):
    """Seeds every kind of related record the deletion must cascade
    through: articles (+classification), an import batch, a narrative
    generation, and a chat session.
    """
    articles = article_factory(project, count=2, retailer="Auchan")
    classification_factory(articles[0])

    batch = start_import_batch(db_session, project.id)

    generation, _is_new = create_project_generation(db_session, project, AnalyticsFilters())
    session = find_or_create_project_session(db_session, project)

    return {
        "article_ids": [a.id for a in articles],
        "batch_id": batch.id,
        "generation_id": generation.id,
        "chat_session_id": session.id,
    }


# --- full cascade deletion ---------------------------------------------------


def test_delete_removes_project_and_all_related_records(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory(name="Full Cascade Project")
    seeded = _fully_populate_project(db_session, project, article_factory, classification_factory)
    project_id = project.id

    response = authenticated_client.post(
        f"/projects/{project_id}/delete",
        data={"confirm_name": "Full Cascade Project"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?deleted=")

    # The delete happened on the request's own session, not this fixture's
    # -- discard this session's stale identity-map cache before re-reading.
    db_session.rollback()

    assert db_session.get(Project, project_id) is None
    assert db_session.query(Article).filter(Article.id.in_(seeded["article_ids"])).count() == 0
    assert db_session.query(Classification).filter_by(project_id=project_id).count() == 0
    assert db_session.get(ImportBatch, seeded["batch_id"]) is None
    assert db_session.get(NarrativeGeneration, seeded["generation_id"]) is None
    assert db_session.get(ChatSession, seeded["chat_session_id"]) is None
    assert db_session.query(UploadedFile).filter_by(project_id=project_id).count() == 0


def test_delete_removes_the_upload_directory(
    authenticated_client, db_session, project_factory, standard_workbook_path
):
    project = project_factory(name="Upload Dir Project")
    authenticated_client.post(
        f"/projects/{project.id}/files",
        files=[("files", ("Auchan Q2 2026.xlsx", standard_workbook_path.read_bytes(), XLSX_CONTENT_TYPE))],
    )
    uploaded_file = db_session.query(UploadedFile).filter_by(project_id=project.id).one()
    project_dir = Path(uploaded_file.stored_path).parent
    assert project_dir.exists()
    assert project_dir.name == str(project.id)

    response = authenticated_client.post(
        f"/projects/{project.id}/delete",
        data={"confirm_name": "Upload Dir Project"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not project_dir.exists()


def test_delete_never_touches_another_projects_directory(
    authenticated_client, db_session, project_factory, standard_workbook_path, penny_workbook_path
):
    project_a = project_factory(name="Project A Keep")
    project_b = project_factory(name="Project B Delete")

    authenticated_client.post(
        f"/projects/{project_a.id}/files",
        files=[("files", ("Auchan Q2 2026.xlsx", standard_workbook_path.read_bytes(), XLSX_CONTENT_TYPE))],
    )
    authenticated_client.post(
        f"/projects/{project_b.id}/files",
        files=[("files", ("Penny - Rewe Q2 2026.xlsx", penny_workbook_path.read_bytes(), XLSX_CONTENT_TYPE))],
    )

    file_a = db_session.query(UploadedFile).filter_by(project_id=project_a.id).one()
    file_b = db_session.query(UploadedFile).filter_by(project_id=project_b.id).one()
    dir_a = Path(file_a.stored_path).parent
    dir_b = Path(file_b.stored_path).parent
    assert dir_a.exists()
    assert dir_b.exists()

    response = authenticated_client.post(
        f"/projects/{project_b.id}/delete",
        data={"confirm_name": "Project B Delete"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    assert not dir_b.exists()
    assert dir_a.exists()  # untouched
    assert db_session.get(Project, project_a.id) is not None  # untouched
    articles_a = db_session.query(Article).filter_by(project_id=project_a.id).all()
    assert len(articles_a) > 0  # project A's data is fully intact


# --- authentication / authorization ------------------------------------------


def test_delete_requires_authentication(client, db_session, project_factory):
    project = project_factory(name="Auth Required Project")

    response = client.post(
        f"/projects/{project.id}/delete",
        data={"confirm_name": "Auth Required Project"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/login"
    assert db_session.get(Project, project.id) is not None


# --- confirmation gate ---------------------------------------------------------


def test_delete_rejected_when_confirmation_name_does_not_match(
    authenticated_client, db_session, project_factory
):
    project = project_factory(name="Name Mismatch Project")

    response = authenticated_client.post(
        f"/projects/{project.id}/delete",
        data={"confirm_name": "the wrong name"},
    )

    assert response.status_code == 422
    assert "did not match" in response.text.lower()
    assert db_session.get(Project, project.id) is not None


def test_delete_rejected_with_blank_confirmation(authenticated_client, db_session, project_factory):
    project = project_factory(name="Blank Confirm Project")

    response = authenticated_client.post(f"/projects/{project.id}/delete", data={})

    assert response.status_code == 422
    assert db_session.get(Project, project.id) is not None


# --- malformed / missing project ids -----------------------------------------


def test_delete_with_malformed_project_id_returns_422(authenticated_client):
    response = authenticated_client.post(
        "/projects/not-a-valid-uuid/delete", data={"confirm_name": "anything"}
    )
    assert response.status_code == 422


def test_delete_with_missing_project_returns_404(authenticated_client):
    missing_id = uuid.uuid4()
    response = authenticated_client.post(
        f"/projects/{missing_id}/delete", data={"confirm_name": "anything"}
    )
    assert response.status_code == 404


# --- success feedback ----------------------------------------------------------


def test_delete_redirects_to_projects_page_with_success_message(
    authenticated_client, db_session, project_factory
):
    project = project_factory(name="Success Message Project")

    delete_response = authenticated_client.post(
        f"/projects/{project.id}/delete",
        data={"confirm_name": "Success Message Project"},
        follow_redirects=False,
    )
    assert delete_response.status_code == 303

    follow_up = authenticated_client.get(delete_response.headers["location"])
    assert follow_up.status_code == 200
    assert "Success Message Project" in follow_up.text
    assert "was deleted" in follow_up.text
