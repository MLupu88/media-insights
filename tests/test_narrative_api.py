from unittest.mock import patch

import httpx

from app.services.narrative_contract import NarrativeTypes


def _mock_response(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code=status_code, request=httpx.Request("POST", "https://example.test")
    )


def _create_generation(db_session, project, article_factory, narrative_types=None):
    from app.services.analytics import AnalyticsFilters
    from app.services.narrative_service import create_project_generation

    article_factory(project, count=1, retailer="Auchan")
    generation, _ = create_project_generation(
        db_session, project, AnalyticsFilters(), narrative_types=narrative_types
    )
    return generation


def _valid_candidate(unique_valid, **overrides) -> dict:
    base = {
        "narrative_type": "executive_summary",
        "key": "main",
        "title": "Title",
        "narrative": "Narrative text.",
        "evidence_type": "kpi_delta",
        "evidence": [{"path": "kpis.unique_valid_articles", "role": "value", "value": unique_valid}],
        "related_article_ids": [],
        "source_urls": [],
    }
    base.update(overrides)
    return base


# --- Internal API -----------------------------------------------------------


def test_payload_endpoint_requires_internal_secret(client, project_factory, article_factory, db_session):
    project = project_factory()
    generation = _create_generation(db_session, project, article_factory)

    response = client.get(f"/api/internal/narrative-generations/{generation.id}/payload")
    assert response.status_code == 401


def test_payload_endpoint_returns_persisted_snapshot_verbatim(
    client, internal_headers, project_factory, article_factory, db_session
):
    project = project_factory()
    generation = _create_generation(db_session, project, article_factory)

    # Data changes after the generation (and its snapshot) were created.
    article_factory(project, count=10, retailer="Carrefour")

    response = client.get(
        f"/api/internal/narrative-generations/{generation.id}/payload", headers=internal_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source_snapshot"] == generation.source_snapshot
    assert body["source_snapshot"]["data"]["kpis"]["unique_valid_articles"] == 1


def test_results_endpoint_validates_and_persists(
    client, internal_headers, project_factory, article_factory, db_session
):
    project = project_factory()
    generation = _create_generation(
        db_session, project, article_factory, narrative_types=["executive_summary", "key_findings"]
    )
    unique_valid = generation.source_snapshot["data"]["kpis"]["unique_valid_articles"]

    submission = {
        "model": "deepseek-chat",
        "prompt_version": "v1",
        "payload_schema_version": generation.payload_schema_version,
        "insights": [
            _valid_candidate(unique_valid),
            _valid_candidate(999999, narrative_type="key_findings", key="bad"),
        ],
    }
    response = client.post(
        f"/api/internal/narrative-generations/{generation.id}/results",
        json=submission,
        headers=internal_headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "partially_complete"
    assert response.json()["missing_narrative_types"] == ["key_findings"]


def test_results_endpoint_rejects_mismatched_schema_version(
    client, internal_headers, project_factory, article_factory, db_session
):
    project = project_factory()
    generation = _create_generation(db_session, project, article_factory)

    submission = {
        "model": "deepseek-chat",
        "prompt_version": "v1",
        "payload_schema_version": "wrong-version",
        "insights": [],
    }
    response = client.post(
        f"/api/internal/narrative-generations/{generation.id}/results",
        json=submission,
        headers=internal_headers,
    )
    assert response.status_code == 422


def test_standard_endpoint_excludes_rejected_insights(
    client, internal_headers, project_factory, article_factory, db_session
):
    project = project_factory()
    generation = _create_generation(db_session, project, article_factory)
    unique_valid = generation.source_snapshot["data"]["kpis"]["unique_valid_articles"]

    submission = {
        "model": "deepseek-chat",
        "prompt_version": "v1",
        "payload_schema_version": generation.payload_schema_version,
        "insights": [
            _valid_candidate(unique_valid, key="good", title="Good title"),
            _valid_candidate(999999, key="bad", title="Bad title"),
        ],
    }
    client.post(
        f"/api/internal/narrative-generations/{generation.id}/results",
        json=submission,
        headers=internal_headers,
    )

    response = client.get(
        f"/api/internal/narrative-generations/{generation.id}", headers=internal_headers
    )
    titles = [i["title"] for i in response.json()["insights"]]
    assert titles == ["Good title"]


def test_audit_endpoint_includes_rejected_with_reason(
    client, internal_headers, project_factory, article_factory, db_session
):
    project = project_factory()
    generation = _create_generation(db_session, project, article_factory)

    submission = {
        "model": "deepseek-chat",
        "prompt_version": "v1",
        "payload_schema_version": generation.payload_schema_version,
        "insights": [_valid_candidate(999999, key="bad", title="Bad title")],
    }
    client.post(
        f"/api/internal/narrative-generations/{generation.id}/results",
        json=submission,
        headers=internal_headers,
    )

    response = client.get(
        f"/api/internal/narrative-generations/{generation.id}/audit", headers=internal_headers
    )
    body = response.json()
    assert len(body["insights"]) == 1
    assert body["insights"][0]["validation_status"] == "rejected"
    assert body["insights"][0]["rejection_reason"] is not None


def test_status_endpoint(client, internal_headers, project_factory, article_factory, db_session):
    project = project_factory()
    generation = _create_generation(db_session, project, article_factory)

    response = client.get(
        f"/api/internal/narrative-generations/{generation.id}/status", headers=internal_headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_list_project_narrative_generations(
    client, internal_headers, project_factory, article_factory, db_session
):
    project = project_factory()
    _create_generation(db_session, project, article_factory)

    response = client.get(
        f"/api/internal/projects/{project.id}/narrative-generations", headers=internal_headers
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["scope"] == "project"


# --- Browser routes ----------------------------------------------------------


def test_start_project_narrative_requires_session(client, project_factory):
    project = project_factory()
    response = client.post(f"/projects/{project.id}/narratives/start", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_start_project_narrative_not_found(authenticated_client):
    import uuid

    response = authenticated_client.post(f"/projects/{uuid.uuid4()}/narratives/start")
    assert response.status_code == 404


@patch("app.services.n8n.httpx.post")
def test_start_project_narrative_success(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    response = authenticated_client.post(f"/projects/{project.id}/narratives/start")

    assert response.status_code == 200
    assert "Narrative generation started" in response.text
    assert mock_post.called
    _, kwargs = mock_post.call_args
    assert "secret" in kwargs["json"]
    assert "test-internal-secret" not in response.text


@patch("app.services.n8n.httpx.post")
def test_start_project_narrative_timeout_marks_failed(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.side_effect = httpx.TimeoutException("timed out")
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    response = authenticated_client.post(f"/projects/{project.id}/narratives/start")

    assert response.status_code == 502
    assert "timed out" in response.text.lower()


@patch("app.services.n8n.httpx.post")
def test_start_project_narrative_dedup_reuses_completed_generation(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    from app.models.narrative import NarrativeGeneration, NarrativeGenerationStatus

    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    first_response = authenticated_client.post(f"/projects/{project.id}/narratives/start")
    assert first_response.status_code == 200
    assert mock_post.call_count == 1

    generation = db_session.query(NarrativeGeneration).filter_by(project_id=project.id).one()
    generation.status = NarrativeGenerationStatus.COMPLETE
    db_session.commit()

    second_response = authenticated_client.post(f"/projects/{project.id}/narratives/start")
    assert second_response.status_code == 200
    assert "Reused an existing narrative generation" in second_response.text
    # n8n was not called again for the unchanged input.
    assert mock_post.call_count == 1


@patch("app.services.n8n.httpx.post")
def test_start_project_narrative_force_regenerate_creates_lineage(
    mock_post, authenticated_client, internal_headers, db_session, project_factory, article_factory
):
    from app.models.narrative import NarrativeGeneration, NarrativeGenerationStatus

    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    authenticated_client.post(f"/projects/{project.id}/narratives/start")
    first = db_session.query(NarrativeGeneration).filter_by(project_id=project.id).one()
    first.status = NarrativeGenerationStatus.COMPLETE
    db_session.commit()

    authenticated_client.post(
        f"/projects/{project.id}/narratives/start", data={"force_regenerate": "true"}
    )

    all_generations = (
        db_session.query(NarrativeGeneration).filter_by(project_id=project.id).all()
    )
    assert len(all_generations) == 2
    second = next(g for g in all_generations if g.id != first.id)
    assert second.regenerated_from_generation_id == first.id


@patch("app.services.n8n.httpx.post")
def test_start_comparison_narrative_redirects_to_detail_page(
    mock_post, authenticated_client, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    response = authenticated_client.post(
        "/compare/narratives/start",
        data={"baseline_project_ids": [str(a.id)], "comparison_project_ids": [str(b.id)]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/narrative-generations/")

    detail_response = authenticated_client.get(response.headers["location"])
    assert detail_response.status_code == 200
    assert "Comparison narrative generation" in detail_response.text


@patch("app.services.n8n.httpx.post")
def test_comparison_generation_uses_comparison_scope_defaults(
    mock_post, authenticated_client, internal_headers, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    response = authenticated_client.post(
        "/compare/narratives/start",
        data={"baseline_project_ids": [str(a.id)], "comparison_project_ids": [str(b.id)]},
        follow_redirects=False,
    )
    generation_id = response.headers["location"].rsplit("/", 1)[-1]

    api_response = authenticated_client.get(
        f"/api/internal/narrative-generations/{generation_id}", headers=internal_headers
    )
    assert set(api_response.json()["narrative_types"]) == set(
        NarrativeTypes.COMPARISON_SCOPE_DEFAULTS
    )


def test_rejected_insight_never_appears_on_detail_page(
    authenticated_client, internal_headers, project_factory, article_factory, db_session
):
    project = project_factory()
    generation = _create_generation(db_session, project, article_factory)

    submission = {
        "model": "deepseek-chat",
        "prompt_version": "v1",
        "payload_schema_version": generation.payload_schema_version,
        "insights": [
            _valid_candidate(999999, key="bad", title="Rejected Title Should Not Appear"),
        ],
    }
    authenticated_client.post(
        f"/api/internal/narrative-generations/{generation.id}/results",
        json=submission,
        headers=internal_headers,
    )

    response = authenticated_client.get(f"/narrative-generations/{generation.id}")
    assert response.status_code == 200
    assert "Rejected Title Should Not Appear" not in response.text
