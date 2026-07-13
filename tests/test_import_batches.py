from app.models.import_batch import ImportBatch, ImportBatchStatus
from app.models.project import Project
from app.models.uploaded_file import UploadedFile
from app.services.imports import fail_import_batch, start_import_batch

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _create_project(client, db_session, name="Batch Test Project", quarter="2026-Q2") -> Project:
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


def test_batch_created_for_a_single_file_upload(
    authenticated_client, db_session, standard_workbook_path
):
    project = _create_project(authenticated_client, db_session)

    authenticated_client.post(
        f"/projects/{project.id}/files",
        files=_files_payload((standard_workbook_path, "Auchan Q2 2026.xlsx")),
    )

    batches = db_session.query(ImportBatch).filter_by(project_id=project.id).all()
    assert len(batches) == 1
    batch = batches[0]
    assert batch.status == ImportBatchStatus.COMPLETED
    assert batch.completed_at is not None
    assert batch.files_processed == 1
    assert batch.files_accepted == 1
    assert batch.files_rejected == 0
    assert batch.total_rows == 5
    assert batch.valid_rows == 4
    assert batch.invalid_rows == 1
    assert batch.duplicate_rows == 1
    assert batch.error_summary is None

    uploaded_file = db_session.query(UploadedFile).filter_by(project_id=project.id).one()
    assert uploaded_file.import_batch_id == batch.id


def test_batch_completed_when_all_files_in_a_multi_file_request_succeed(
    authenticated_client, db_session, standard_workbook_path, penny_workbook_path
):
    project = _create_project(authenticated_client, db_session)

    authenticated_client.post(
        f"/projects/{project.id}/files",
        files=_files_payload(
            (standard_workbook_path, "Auchan Q2 2026.xlsx"),
            (penny_workbook_path, "Penny - Rewe Q2 2026.xlsx"),
        ),
    )

    batch = db_session.query(ImportBatch).filter_by(project_id=project.id).one()
    assert batch.status == ImportBatchStatus.COMPLETED
    assert batch.files_processed == 2
    assert batch.files_accepted == 2
    assert batch.files_rejected == 0
    assert batch.error_summary is None


def test_batch_failed_when_every_file_is_rejected(authenticated_client, db_session):
    project = _create_project(authenticated_client, db_session)

    response = authenticated_client.post(
        f"/projects/{project.id}/files",
        files=[("files", ("notes.txt", b"just some text", "text/plain"))],
    )
    assert response.status_code == 422

    batch = db_session.query(ImportBatch).filter_by(project_id=project.id).one()
    assert batch.status == ImportBatchStatus.FAILED
    assert batch.files_processed == 1
    assert batch.files_rejected == 1
    assert batch.files_accepted == 0
    assert batch.completed_at is not None
    assert "notes.txt" in batch.error_summary


def test_batch_partially_completed_with_one_valid_and_one_rejected_file(
    authenticated_client, db_session, standard_workbook_path
):
    project = _create_project(authenticated_client, db_session)

    response = authenticated_client.post(
        f"/projects/{project.id}/files",
        files=[
            ("files", ("Auchan Q2 2026.xlsx", standard_workbook_path.read_bytes(), XLSX_CONTENT_TYPE)),
            ("files", ("notes.txt", b"just some text", "text/plain")),
        ],
    )
    assert response.status_code == 200

    batch = db_session.query(ImportBatch).filter_by(project_id=project.id).one()
    assert batch.status == ImportBatchStatus.PARTIALLY_COMPLETED
    assert batch.files_processed == 2
    assert batch.files_accepted == 1
    assert batch.files_rejected == 1
    assert batch.completed_at is not None
    assert "notes.txt" in batch.error_summary


def test_batch_partially_completed_when_a_file_fails_to_parse(
    authenticated_client, db_session, standard_workbook_path
):
    project = _create_project(authenticated_client, db_session)
    broken_bytes = b"this is not a real xlsx file"

    response = authenticated_client.post(
        f"/projects/{project.id}/files",
        files=[
            ("files", ("Auchan Q2 2026.xlsx", standard_workbook_path.read_bytes(), XLSX_CONTENT_TYPE)),
            ("files", ("Broken.xlsx", broken_bytes, XLSX_CONTENT_TYPE)),
        ],
    )
    assert response.status_code == 200

    batch = db_session.query(ImportBatch).filter_by(project_id=project.id).one()
    assert batch.status == ImportBatchStatus.PARTIALLY_COMPLETED
    assert batch.files_accepted == 1
    assert batch.files_rejected == 1
    assert "Broken.xlsx" in batch.error_summary


def test_batch_needs_review_rows_counts_unresolved_articles(
    authenticated_client, db_session, standard_workbook_path
):
    project = _create_project(authenticated_client, db_session)

    # No brand column in this fixture and a filename that matches no
    # canonical retailer — every row has no signal at all, so every row
    # (valid, invalid, and duplicate alike) lands in needs_review.
    response = authenticated_client.post(
        f"/projects/{project.id}/files",
        files=[("files", ("Unbranded Coverage Report.xlsx", standard_workbook_path.read_bytes(), XLSX_CONTENT_TYPE))],
    )
    assert response.status_code == 200

    batch = db_session.query(ImportBatch).filter_by(project_id=project.id).one()
    assert batch.needs_review_rows == 5


# --- "hard crash" state --------------------------------------------------


def test_batch_left_processing_when_finalization_never_runs(db_session, project_factory):
    """Simulates a hard process crash: nothing ever finalizes the batch,
    so it must remain visibly `processing` with no `completed_at` — no
    code needs to detect this specially, it's the natural consequence of
    finalization never running.
    """
    project = project_factory()
    batch = start_import_batch(db_session, project.id)

    db_session.refresh(batch)
    assert batch.status == ImportBatchStatus.PROCESSING
    assert batch.completed_at is None


def test_fail_import_batch_marks_failed_with_completed_at_set(db_session, project_factory):
    project = project_factory()
    batch = start_import_batch(db_session, project.id)

    fail_import_batch(db_session, batch.id, "Simulated crash mid-loop")

    db_session.refresh(batch)
    assert batch.status == ImportBatchStatus.FAILED
    assert batch.completed_at is not None
    assert batch.error_summary == "Simulated crash mid-loop"


def test_batch_finalized_as_failed_when_an_unexpected_exception_escapes_the_loop(
    authenticated_client, db_session, standard_workbook_path, monkeypatch
):
    """The upload endpoint never re-raises to a raw 500 -- it renders the
    Files page with a readable error and still finalizes the batch as
    failed (Part 3 hotfix: see tests/test_import_error_handling.py for the
    full regression suite around this behavior).
    """
    project = _create_project(authenticated_client, db_session)

    def _boom(db, uploaded_file):
        raise RuntimeError("simulated unexpected failure")

    import app.api.files as files_module

    monkeypatch.setattr(files_module, "import_uploaded_file", _boom)

    response = authenticated_client.post(
        f"/projects/{project.id}/files",
        files=_files_payload((standard_workbook_path, "Auchan Q2 2026.xlsx")),
    )
    assert response.status_code == 500
    assert "simulated unexpected failure" not in response.text

    batch = db_session.query(ImportBatch).filter_by(project_id=project.id).one()
    assert batch.status == ImportBatchStatus.FAILED
    assert batch.completed_at is not None


# --- internal ingestion path leaves import_batch_id unset --------------------


def test_internal_import_path_does_not_create_a_batch(db_session, project_factory, internal_headers, client):
    project = project_factory()

    response = client.post(
        "/api/internal/import-file",
        json={
            "project_id": str(project.id),
            "quarter": project.quarter,
            "file_path": "/nonexistent/path.xlsx",
            "uploaded_name": "whatever.xlsx",
        },
        headers=internal_headers,
    )
    # Expected to fail path resolution (file doesn't exist) — the point of
    # this test is only that no ImportBatch row is ever created by this
    # ingestion path, regardless of outcome.
    assert response.status_code in (400, 404, 422)
    assert db_session.query(ImportBatch).filter_by(project_id=project.id).count() == 0
