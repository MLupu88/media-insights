from app.models.project import AnalysisStatus, Project, ProjectStatus


def test_projects_page_shows_empty_state_when_no_projects(authenticated_client):
    response = authenticated_client.get("/")

    assert response.status_code == 200
    assert "No projects yet" in response.text


def test_projects_page_requires_authentication(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_create_project_success(authenticated_client, db_session):
    response = authenticated_client.post(
        "/projects",
        data={
            "name": "Auchan Q2 2026",
            "quarter": "2026-Q2",
            "description": "Retail media monitoring",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    project = db_session.query(Project).filter_by(name="Auchan Q2 2026").one()
    assert project.quarter == "2026-Q2"
    assert project.description == "Retail media monitoring"
    assert project.status == ProjectStatus.CREATED
    assert project.analysis_status == AnalysisStatus.NOT_STARTED
    assert project.total_files == 0
    assert project.total_rows == 0
    assert project.valid_rows == 0
    assert project.invalid_rows == 0
    assert project.duplicate_rows == 0
    assert project.classified_rows == 0
    assert project.created_at is not None
    assert project.updated_at is not None


def test_create_project_without_description(authenticated_client, db_session):
    response = authenticated_client.post(
        "/projects",
        data={"name": "Lidl Q3 2026", "quarter": "2026-Q3", "description": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303

    project = db_session.query(Project).filter_by(name="Lidl Q3 2026").one()
    assert project.description is None


def test_create_project_requires_authentication(client):
    response = client.post(
        "/projects",
        data={"name": "Metro Q1 2026", "quarter": "2026-Q1"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_create_project_rejects_invalid_quarter(authenticated_client, db_session):
    response = authenticated_client.post(
        "/projects",
        data={"name": "Profi Q2 2026", "quarter": "Q2-2026"},
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert "Quarter must be in the format" in response.text
    assert db_session.query(Project).filter_by(name="Profi Q2 2026").first() is None


def test_create_project_rejects_blank_name(authenticated_client, db_session):
    response = authenticated_client.post(
        "/projects",
        data={"name": "", "quarter": "2026-Q2"},
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert db_session.query(Project).count() == 0


def test_projects_page_lists_created_project(authenticated_client):
    authenticated_client.post(
        "/projects",
        data={"name": "Carrefour Q2 2026", "quarter": "2026-Q2"},
        follow_redirects=False,
    )

    response = authenticated_client.get("/")

    assert response.status_code == 200
    assert "Carrefour Q2 2026" in response.text
    assert "2026-Q2" in response.text
    assert "No projects yet" not in response.text


def test_project_detail_page_renders_for_existing_project(authenticated_client, db_session):
    authenticated_client.post(
        "/projects",
        data={"name": "Kaufland Q2 2026", "quarter": "2026-Q2"},
        follow_redirects=False,
    )
    project = db_session.query(Project).filter_by(name="Kaufland Q2 2026").one()

    response = authenticated_client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert "Kaufland Q2 2026" in response.text


def test_project_detail_page_404_for_unknown_project(authenticated_client):
    response = authenticated_client.get(
        "/projects/00000000-0000-0000-0000-000000000000"
    )

    assert response.status_code == 404
