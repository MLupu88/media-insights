def test_analytics_tab_empty_state_before_any_files(authenticated_client, project_factory):
    project = project_factory()

    response = authenticated_client.get(f"/projects/{project.id}?tab=analytics")

    assert response.status_code == 200
    assert "No files uploaded yet" in response.text


def test_analytics_tab_empty_state_when_no_valid_articles(
    authenticated_client, db_session, project_factory
):
    project = project_factory()
    project.total_rows = 3
    project.valid_rows = 0
    project.invalid_rows = 3
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=analytics")

    assert response.status_code == 200
    assert "No valid articles to analyze yet" in response.text


def test_analytics_tab_renders_filter_form_and_kpis(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=2, retailer="Auchan", audience=1000.0)
    project.total_rows = 2
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=analytics")

    assert response.status_code == 200
    assert 'name="brand"' in response.text  # checkbox multi-brand filter (Phase D)
    assert 'id="filter-sentiment"' in response.text
    assert "Unique valid articles" in response.text
    assert "Auchan" in response.text


def test_analytics_tab_shows_unclassified_note_when_nothing_classified(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1)
    project.total_rows = 1
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=analytics")

    assert response.status_code == 200
    assert "No classified articles in the current filter selection yet" in response.text


def test_analytics_tab_requires_authentication(client, project_factory):
    project = project_factory()

    response = client.get(f"/projects/{project.id}?tab=analytics", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_analytics_tab_does_not_leak_internal_secret(
    authenticated_client, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1)

    response = authenticated_client.get(f"/projects/{project.id}?tab=analytics")

    assert "test-internal-secret" not in response.text


def test_analytics_tab_and_internal_api_report_matching_figures(
    authenticated_client,
    internal_headers,
    db_session,
    project_factory,
    article_factory,
    classification_factory,
):
    """API/UI consistency: both surfaces call the same shared service, so the
    numbers they show for the same project must match exactly.
    """
    project = project_factory()
    articles = article_factory(project, count=3, retailer="Auchan", audience=2000.0)
    classification_factory(articles[0], primary_topic="store_expansion", sentiment="positive")
    classification_factory(articles[1], primary_topic="promotions_pricing", sentiment="negative")
    project.total_rows = 3
    db_session.commit()

    ui_response = authenticated_client.get(f"/projects/{project.id}?tab=analytics")
    api_response = authenticated_client.get(
        f"/api/internal/projects/{project.id}/analytics", headers=internal_headers
    )

    assert ui_response.status_code == 200
    assert api_response.status_code == 200
    api_body = api_response.json()

    assert f"{api_body['kpis']['unique_valid_articles']}" in ui_response.text
    assert f"{api_body['kpis']['total_reach']:,.0f}" in ui_response.text
    for brand_row in api_body["brands"]["by_volume"]:
        assert brand_row["brand"] in ui_response.text
        assert f"{brand_row['sov_pct']}%" in ui_response.text


def test_analytics_tab_filter_options_stable_across_filtered_request(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan", source="Ziarul")
    article_factory(project, count=1, retailer="Carrefour", source="Adevarul")
    project.total_rows = 2
    db_session.commit()

    response = authenticated_client.get(
        f"/projects/{project.id}?tab=analytics&brand=Auchan"
    )

    assert response.status_code == 200
    # Both publications must still be listed even though we filtered by brand.
    assert "Ziarul" in response.text
    assert "Adevarul" in response.text


def test_analytics_nav_link_present_on_other_tabs(authenticated_client, project_factory):
    project = project_factory()

    response = authenticated_client.get(f"/projects/{project.id}")

    assert response.status_code == 200
    assert 'data-tab-indicator="analytics"' in response.text
    assert 'data-tab-panel="analytics"' in response.text
