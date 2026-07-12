import shutil
import uuid

from app.models.article import Article
from app.models.uploaded_file import UploadedFile, UploadedFileStatus
from app.services.storage import project_upload_dir

IMPORT_FILE_URL = "/api/internal/import-file"


def _stage_workbook(project_id, workbook_path, filename="Auchan Q2 2026.xlsx"):
    directory = project_upload_dir(project_id)
    destination = directory / filename
    shutil.copy(workbook_path, destination)
    return destination


def test_import_file_requires_internal_secret(client, project_factory, standard_workbook_path):
    project = project_factory()
    stored_path = _stage_workbook(project.id, standard_workbook_path)

    response = client.post(
        IMPORT_FILE_URL,
        json={
            "project_id": str(project.id),
            "quarter": project.quarter,
            "file_path": str(stored_path),
            "uploaded_name": "Auchan Q2 2026.xlsx",
            "retailer_hint": "Auchan",
        },
    )

    assert response.status_code == 401


def test_import_file_reuses_phase2_import_logic(
    client, internal_headers, db_session, project_factory, standard_workbook_path
):
    project = project_factory()
    stored_path = _stage_workbook(project.id, standard_workbook_path)

    response = client.post(
        IMPORT_FILE_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "quarter": project.quarter,
            "file_path": str(stored_path),
            "uploaded_name": "Auchan Q2 2026.xlsx",
            "retailer_hint": "Auchan",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "imported"
    assert body["retailer"] == "Auchan"
    assert body["rows_received"] == 5
    assert body["rows_imported"] == 4
    assert body["rows_invalid"] == 1
    assert body["rows_duplicate"] == 1

    uploaded_file = db_session.query(UploadedFile).filter_by(project_id=project.id).one()
    assert uploaded_file.status == UploadedFileStatus.COMPLETED
    articles = db_session.query(Article).filter_by(uploaded_file_id=uploaded_file.id).all()
    assert len(articles) == 5


def test_import_file_project_not_found(client, internal_headers, standard_workbook_path, tmp_path):
    fake_project_id = uuid.uuid4()
    response = client.post(
        IMPORT_FILE_URL,
        headers=internal_headers,
        json={
            "project_id": str(fake_project_id),
            "quarter": "2026-Q2",
            "file_path": str(standard_workbook_path),
            "uploaded_name": "Auchan Q2 2026.xlsx",
        },
    )
    assert response.status_code == 404


def test_import_file_rejects_path_traversal(client, internal_headers, project_factory):
    project = project_factory()

    response = client.post(
        IMPORT_FILE_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "quarter": project.quarter,
            "file_path": "../../../../etc/passwd",
            "uploaded_name": "passwd",
        },
    )

    assert response.status_code == 400
    assert "upload directory" in response.json()["detail"]


def test_import_file_rejects_path_traversal_with_dotdot_inside_root(
    client, internal_headers, project_factory
):
    project = project_factory()
    directory = project_upload_dir(project.id)

    traversal_path = str(directory / ".." / ".." / "outside.xlsx")

    response = client.post(
        IMPORT_FILE_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "quarter": project.quarter,
            "file_path": traversal_path,
            "uploaded_name": "outside.xlsx",
        },
    )

    assert response.status_code == 400


def test_import_file_rejects_non_xlsx(client, internal_headers, project_factory, tmp_path):
    project = project_factory()
    directory = project_upload_dir(project.id)
    text_file = directory / "notes.txt"
    text_file.write_text("just some text")

    response = client.post(
        IMPORT_FILE_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "quarter": project.quarter,
            "file_path": str(text_file),
            "uploaded_name": "notes.txt",
        },
    )

    assert response.status_code == 400
    assert "xlsx" in response.json()["detail"].lower()


def test_import_file_rejects_missing_file(client, internal_headers, project_factory):
    project = project_factory()
    directory = project_upload_dir(project.id)
    missing_path = directory / "does-not-exist.xlsx"

    response = client.post(
        IMPORT_FILE_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "quarter": project.quarter,
            "file_path": str(missing_path),
            "uploaded_name": "does-not-exist.xlsx",
        },
    )

    assert response.status_code == 404


def test_import_file_reuses_existing_completed_upload_without_reimporting(
    client, internal_headers, db_session, project_factory, standard_workbook_path
):
    project = project_factory()
    stored_path = _stage_workbook(project.id, standard_workbook_path)

    payload = {
        "project_id": str(project.id),
        "quarter": project.quarter,
        "file_path": str(stored_path),
        "uploaded_name": "Auchan Q2 2026.xlsx",
    }

    first = client.post(IMPORT_FILE_URL, headers=internal_headers, json=payload)
    assert first.status_code == 200
    first_uploaded_file_id = first.json()["uploaded_file_id"]

    second = client.post(IMPORT_FILE_URL, headers=internal_headers, json=payload)
    assert second.status_code == 200
    assert second.json()["uploaded_file_id"] == first_uploaded_file_id

    # Article count must not double from re-calling the endpoint on the same file.
    articles = db_session.query(Article).filter_by(uploaded_file_id=first_uploaded_file_id).all()
    assert len(articles) == 5
