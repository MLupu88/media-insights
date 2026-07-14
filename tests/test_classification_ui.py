import re


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


def test_start_classification_button_reenables_when_status_is_stale_running_with_no_active_batch(
    authenticated_client, db_session, project_factory, article_factory
):
    """analysis_status can be left at "running" if the async batch
    continuation never runs (see test_classification_n8n_trigger.py) -- the
    button must be driven by live active-batch data, not that stale flag.
    """
    project = project_factory()
    article_factory(project, count=4)
    project.valid_rows = 4
    project.classified_rows = 0
    project.analysis_status = "running"
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=classification")

    assert response.status_code == 200
    button_start = response.text.index("Start classification")
    button_markup_start = response.text.rindex("<button", 0, button_start)
    button_markup = response.text[button_markup_start:button_start]
    # The button's class list legitimately contains Tailwind
    # "disabled:..." variant selectors -- only a bare `disabled` HTML
    # attribute (not followed by ":") means the button is actually disabled.
    assert re.search(r"\bdisabled\b(?!:)", button_markup) is None
