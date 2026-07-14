from unittest.mock import patch

import httpx

from app.services.analytics import AnalyticsFilters
from app.services.narrative_service import create_project_generation


def _mock_response(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code=status_code, request=httpx.Request("POST", "https://example.test")
    )


def test_insights_tab_empty_state_when_no_valid_articles(authenticated_client, project_factory):
    project = project_factory()
    response = authenticated_client.get(f"/projects/{project.id}?tab=insights")

    assert response.status_code == 200
    assert "No valid articles to generate insights from yet" in response.text


def test_insights_tab_shows_empty_generations_list(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=insights")

    assert response.status_code == 200
    assert "No narrative generations yet." in response.text
    assert "Generate insights" in response.text


def test_insights_tab_lists_generation_with_status_badge(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()
    create_project_generation(db_session, project, AnalyticsFilters())

    response = authenticated_client.get(f"/projects/{project.id}?tab=insights")

    assert response.status_code == 200
    assert "Pending" in response.text


@patch("app.services.n8n.httpx.post")
def test_insights_tab_generate_button_disabled_while_running(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    authenticated_client.post(f"/projects/{project.id}/narratives/start")
    response = authenticated_client.get(f"/projects/{project.id}?tab=insights")

    assert response.status_code == 200
    assert "disabled" in response.text


def test_narrative_generation_detail_page_shows_valid_insight(
    authenticated_client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    unique_valid = generation.source_snapshot["data"]["kpis"]["unique_valid_articles"]

    submission = {
        "model": "deepseek-chat",
        "prompt_version": "v1",
        "payload_schema_version": generation.payload_schema_version,
        "insights": [
            {
                "narrative_type": "executive_summary",
                "key": "main",
                "title": "Vizibilitate puternica in perioada analizata",
                "narrative": "Textul generat in limba romana.",
                "evidence_type": "kpi_delta",
                "evidence": [
                    {"path": "kpis.unique_valid_articles", "role": "value", "value": unique_valid}
                ],
                "related_article_ids": [],
                "source_urls": [],
            }
        ],
    }
    authenticated_client.post(
        f"/api/internal/narrative-generations/{generation.id}/results",
        json=submission,
        headers=internal_headers,
    )

    response = authenticated_client.get(f"/narrative-generations/{generation.id}")

    assert response.status_code == 200
    assert "Vizibilitate puternica in perioada analizata" in response.text
    assert "Textul generat in limba romana" in response.text


def test_narrative_generation_detail_page_does_not_leak_internal_secret(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())

    response = authenticated_client.get(f"/narrative-generations/{generation.id}")

    assert "test-internal-secret" not in response.text


def test_narrative_generation_detail_page_not_found(authenticated_client):
    import uuid

    response = authenticated_client.get(f"/narrative-generations/{uuid.uuid4()}")
    assert response.status_code == 404


def test_ui_and_internal_api_report_matching_insight_titles(
    authenticated_client, internal_headers, db_session, project_factory, article_factory
):
    """API/UI consistency: the browser detail page and the internal package
    endpoint both read the same valid-only insight set for one generation.
    """
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    unique_valid = generation.source_snapshot["data"]["kpis"]["unique_valid_articles"]

    submission = {
        "model": "deepseek-chat",
        "prompt_version": "v1",
        "payload_schema_version": generation.payload_schema_version,
        "insights": [
            {
                "narrative_type": "executive_summary",
                "key": "main",
                "title": "Consistent Title Example",
                "narrative": "Some narrative.",
                "evidence_type": "kpi_delta",
                "evidence": [
                    {"path": "kpis.unique_valid_articles", "role": "value", "value": unique_valid}
                ],
                "related_article_ids": [],
                "source_urls": [],
            }
        ],
    }
    authenticated_client.post(
        f"/api/internal/narrative-generations/{generation.id}/results",
        json=submission,
        headers=internal_headers,
    )

    ui_response = authenticated_client.get(f"/narrative-generations/{generation.id}")
    api_response = authenticated_client.get(
        f"/api/internal/narrative-generations/{generation.id}", headers=internal_headers
    )

    api_titles = [i["title"] for i in api_response.json()["insights"]]
    assert api_titles == ["Consistent Title Example"]
    assert "Consistent Title Example" in ui_response.text


def test_partially_classified_project_can_still_generate(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    articles = article_factory(project, count=3, retailer="Auchan")
    classification_factory(articles[0])  # only one of three classified

    generation, is_new = create_project_generation(db_session, project, AnalyticsFilters())

    assert is_new
    assert generation.source_snapshot["data"]["kpis"]["unique_classified_articles"] == 1
    assert generation.source_snapshot["data"]["kpis"]["unique_unclassified_articles"] == 2


def test_insights_pending_generation_enables_auto_refresh(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())

    response = authenticated_client.get(f"/projects/{project.id}?tab=insights")

    assert response.status_code == 200
    assert 'data-async-status-poll' in response.text
    assert f'/api/ui/narrative-generations/{generation.id}/status' in response.text
    assert f'/projects/{project.id}?tab=insights' in response.text


def test_narrative_ui_status_is_minimal_and_no_store(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())

    response = authenticated_client.get(
        f"/api/ui/narrative-generations/{generation.id}/status"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "id": str(generation.id),
        "status": "pending",
        "terminal": False,
    }


def test_narrative_ui_status_requires_login(
    client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())

    response = client.get(
        f"/api/ui/narrative-generations/{generation.id}/status",
        follow_redirects=False,
    )

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/login"
