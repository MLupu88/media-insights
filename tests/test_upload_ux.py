from app.models.import_batch import ImportBatch, ImportBatchStatus
from app.models.project import Project

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _create_project(client, db_session, name="Upload UX Project", quarter="2026-Q2") -> Project:
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


# --- drag-and-drop markup + real multi-file fallback -------------------------


def test_files_tab_renders_dropzone_around_the_real_multi_file_input(authenticated_client, db_session):
    project = _create_project(authenticated_client, db_session)

    response = authenticated_client.get(f"/projects/{project.id}?tab=files")
    assert response.status_code == 200
    assert 'data-upload-dropzone' in response.text
    # The real, functional multi-file input must still be present, with
    # its original attributes untouched — drag-and-drop only populates
    # it, it never replaces the no-JS fallback path.
    assert 'type="file"' in response.text
    assert 'name="files"' in response.text
    assert "multiple" in response.text
    assert 'accept=".xlsx"' in response.text
    assert "data-selected-files" in response.text


def test_upload_route_still_accepts_multiple_files_without_any_js(
    authenticated_client, db_session, standard_workbook_path, penny_workbook_path
):
    """The dropzone is progressive enhancement only — a plain multipart
    POST (exactly what a no-JS browser submit produces) must keep working
    unchanged.
    """
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


# --- import history rendering -------------------------------------------------


def test_import_history_renders_batch_after_upload(
    authenticated_client, db_session, standard_workbook_path
):
    project = _create_project(authenticated_client, db_session)

    authenticated_client.post(
        f"/projects/{project.id}/files",
        files=_files_payload((standard_workbook_path, "Auchan Q2 2026.xlsx")),
    )

    response = authenticated_client.get(f"/projects/{project.id}?tab=files")
    assert response.status_code == 200
    assert "Import history" in response.text
    assert "5 total rows" in response.text
    assert "4 valid" in response.text
    assert "1 invalid" in response.text
    assert "1 duplicate" in response.text


def test_import_history_shows_empty_state_before_any_upload(authenticated_client, db_session):
    project = _create_project(authenticated_client, db_session)

    response = authenticated_client.get(f"/projects/{project.id}?tab=files")
    assert response.status_code == 200
    assert "No import batches recorded yet." in response.text


# --- legacy files without a batch remain valid and visible --------------------


def test_legacy_uploaded_file_without_batch_remains_visible_in_files_list(
    authenticated_client, db_session, project_factory, uploaded_file_factory
):
    project = project_factory()
    legacy_file = uploaded_file_factory(
        project, original_filename="Pre-Phase-C-Legacy.xlsx", import_batch_id=None
    )
    assert legacy_file.import_batch_id is None

    response = authenticated_client.get(f"/projects/{project.id}?tab=files")
    assert response.status_code == 200
    assert "Pre-Phase-C-Legacy.xlsx" in response.text


def test_legacy_uploaded_file_without_batch_does_not_break_import_history(
    authenticated_client, db_session, project_factory, uploaded_file_factory
):
    project = project_factory()
    uploaded_file_factory(project, original_filename="Legacy.xlsx", import_batch_id=None)

    response = authenticated_client.get(f"/projects/{project.id}?tab=files")
    assert response.status_code == 200
    # No batches exist for this project (the file predates ImportBatch),
    # so the history section correctly shows its empty state rather than
    # erroring on a file with no associated batch.
    assert "No import batches recorded yet." in response.text


def test_import_batch_status_values_render_with_readable_labels(
    authenticated_client, db_session, project_factory
):
    project = project_factory()
    batch = ImportBatch(project_id=project.id, status=ImportBatchStatus.PARTIALLY_COMPLETED)
    db_session.add(batch)
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=files")
    assert response.status_code == 200
    assert "Partially completed" in response.text
