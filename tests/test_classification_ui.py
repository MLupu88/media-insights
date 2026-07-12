def test_classification_tab_empty_state_before_import(authenticated_client, project_factory):
    project = project_factory()

    response = authenticated_client.get(f"/projects/{project.id}?tab=classification")

    assert response.status_code == 200
    assert "No valid articles to classify yet" in response.text
    assert "Start classification" not in response.text


def test_classification_tab_shows_progress_and_stats(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=4)
    project.valid_rows = 4
    project.classified_rows = 1
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=classification")

    assert response.status_code == 200
    assert "Start classification" in response.text
    assert "Refresh status" in response.text
    assert "VALID ARTICLES" in response.text.upper()
    assert "25.0%" in response.text


def test_classification_tab_is_default_hidden_when_overview_active(
    authenticated_client, project_factory
):
    project = project_factory()

    response = authenticated_client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert 'data-tab-panel="classification"' in response.text
    # Overview should be the active tab by default.
    assert 'data-tab-button="overview"' in response.text


def test_classification_workspace_requires_authentication(client, project_factory):
    project = project_factory()

    response = client.get(f"/projects/{project.id}?tab=classification", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"
