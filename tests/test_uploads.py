import io

from app.config import get_settings
from app.models.article import Article, ImportStatus
from app.models.project import Project, ProjectStatus
from app.models.uploaded_file import UploadedFile, UploadedFileStatus

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _create_project(client, db_session, name="Auchan Q2 2026", quarter="2026-Q2") -> Project:
    response = client.post(
        "/projects", data={"name": name, "quarter": quarter}, follow_redirects=False
    )
    assert response.status_code == 303
    return db_session.query(Project).filter_by(name=name).one()


def _files_payload(*paths_and_names):
    payload = []
    for path, filename in paths_and_names:
        payload.append(("files", (filename, path.read_bytes(), XLSX_CONTENT_TYPE)))
    return payload


def test_upload_valid_workbook_creates_file_and_articles(
    authenticated_client, db_session, standard_workbook_path
):
    project = _create_project(authenticated_client, db_session)

    response = authenticated_client.post(
        f"/projects/{project.id}/files",
        files=_files_payload((standard_workbook_path, "Auchan Q2 2026.xlsx")),
    )

    assert response.status_code == 200

    uploaded_file = db_session.query(UploadedFile).filter_by(project_id=project.id).one()
    assert uploaded_file.status == UploadedFileStatus.COMPLETED
    assert uploaded_file.original_filename == "Auchan Q2 2026.xlsx"
    assert uploaded_file.detected_retailer == "Auchan"
    assert uploaded_file.workbook_sheet == "Monitorizare"
    assert uploaded_file.row_count == 5
    assert uploaded_file.valid_row_count == 4
    assert uploaded_file.invalid_row_count == 1
    assert uploaded_file.duplicate_row_count == 1

    articles = db_session.query(Article).filter_by(uploaded_file_id=uploaded_file.id).all()
    assert len(articles) == 5

    duplicates = [a for a in articles if a.is_duplicate]
    assert len(duplicates) == 1
    assert duplicates[0].duplicate_of_article_id is not None

    invalid = [a for a in articles if a.import_status == ImportStatus.INVALID]
    assert len(invalid) == 1
    assert invalid[0].title is None
    assert invalid[0].source is None


def test_project_summary_updates_after_import(
    authenticated_client, db_session, standard_workbook_path
):
    project = _create_project(authenticated_client, db_session)

    authenticated_client.post(
        f"/projects/{project.id}/files",
        files=_files_payload((standard_workbook_path, "Auchan Q2 2026.xlsx")),
    )

    db_session.refresh(project)
    assert project.total_files == 1
    assert project.total_rows == 5
    assert project.valid_rows == 4
    assert project.invalid_rows == 1
    assert project.duplicate_rows == 1
    assert project.status == ProjectStatus.IMPORTED


def test_upload_multiple_files_updates_aggregate_totals(
    authenticated_client, db_session, standard_workbook_path, penny_workbook_path
):
    project = _create_project(authenticated_client, db_session)

    response = authenticated_client.post(
        f"/projects/{project.id}/files",
        files=_files_payload(
            (standard_workbook_path, "Auchan Q2 2026.xlsx"),
            (penny_workbook_path, "Penny - Rewe Q2 2026.xlsx"),
        ),
    )

    assert response.status_code == 200

    db_session.refresh(project)
    assert project.total_files == 2
    assert project.total_rows == 5 + 2

    files = db_session.query(UploadedFile).filter_by(project_id=project.id).all()
    retailers = {f.original_filename: f.detected_retailer for f in files}
    assert retailers["Auchan Q2 2026.xlsx"] == "Auchan"
    assert retailers["Penny - Rewe Q2 2026.xlsx"] == "Penny / Rewe"


def test_upload_rejects_non_xlsx_extension(authenticated_client, db_session):
    project = _create_project(authenticated_client, db_session)

    response = authenticated_client.post(
        f"/projects/{project.id}/files",
        files=[("files", ("notes.txt", b"just some text", "text/plain"))],
    )

    assert response.status_code == 422
    assert "Only .xlsx files are supported" in response.text
    assert db_session.query(UploadedFile).filter_by(project_id=project.id).count() == 0


def test_upload_rejects_oversized_file(
    authenticated_client, db_session, standard_workbook_path, monkeypatch
):
    project = _create_project(authenticated_client, db_session)
    monkeypatch.setattr(get_settings(), "max_upload_size_bytes", 100)

    response = authenticated_client.post(
        f"/projects/{project.id}/files",
        files=_files_payload((standard_workbook_path, "Auchan Q2 2026.xlsx")),
    )

    assert response.status_code == 422
    assert "exceeds the maximum allowed size" in response.text
    assert db_session.query(UploadedFile).filter_by(project_id=project.id).count() == 0


def test_upload_requires_authentication(client, standard_workbook_path):
    response = client.post(
        "/projects/00000000-0000-0000-0000-000000000000/files",
        files=_files_payload((standard_workbook_path, "Auchan Q2 2026.xlsx")),
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_upload_to_unknown_project_returns_404(authenticated_client, standard_workbook_path):
    response = authenticated_client.post(
        "/projects/00000000-0000-0000-0000-000000000000/files",
        files=_files_payload((standard_workbook_path, "Auchan Q2 2026.xlsx")),
    )

    assert response.status_code == 404


def test_retry_failed_import_succeeds_after_fixing_the_file(
    authenticated_client, db_session, standard_workbook_path
):
    project = _create_project(authenticated_client, db_session)

    # Upload a corrupt file (wrong extension content) so the initial import fails.
    broken_bytes = b"this is not a real xlsx file"
    authenticated_client.post(
        f"/projects/{project.id}/files",
        files=[("files", ("Auchan Q2 2026.xlsx", broken_bytes, XLSX_CONTENT_TYPE))],
    )

    uploaded_file = db_session.query(UploadedFile).filter_by(project_id=project.id).one()
    assert uploaded_file.status == UploadedFileStatus.FAILED
    assert uploaded_file.error_message

    # Fix the stored file on disk, then retry the import through the endpoint.
    with open(uploaded_file.stored_path, "wb") as f:
        f.write(standard_workbook_path.read_bytes())

    response = authenticated_client.post(
        f"/projects/{project.id}/files/{uploaded_file.id}/retry", follow_redirects=False
    )
    assert response.status_code == 303

    db_session.refresh(uploaded_file)
    assert uploaded_file.status == UploadedFileStatus.COMPLETED
    assert uploaded_file.row_count == 5

    articles = db_session.query(Article).filter_by(uploaded_file_id=uploaded_file.id).all()
    assert len(articles) == 5


def test_files_section_lists_uploaded_files(
    authenticated_client, db_session, standard_workbook_path
):
    project = _create_project(authenticated_client, db_session)
    authenticated_client.post(
        f"/projects/{project.id}/files",
        files=_files_payload((standard_workbook_path, "Auchan Q2 2026.xlsx")),
    )

    response = authenticated_client.get(f"/projects/{project.id}?tab=files")

    assert response.status_code == 200
    assert "Auchan Q2 2026.xlsx" in response.text
    assert "Completed" in response.text
