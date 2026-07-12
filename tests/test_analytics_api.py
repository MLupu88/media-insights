import uuid


def _url(project_id, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    base = f"/api/internal/projects/{project_id}/analytics"
    return f"{base}?{query}" if query else base


def test_analytics_endpoint_requires_internal_secret(client, project_factory):
    project = project_factory()
    response = client.get(_url(project.id))
    assert response.status_code == 401


def test_analytics_endpoint_rejects_invalid_secret(client, project_factory):
    project = project_factory()
    response = client.get(_url(project.id), headers={"x-internal-secret": "wrong"})
    assert response.status_code == 401


def test_analytics_endpoint_accepts_valid_secret(client, internal_headers, project_factory):
    project = project_factory()
    response = client.get(_url(project.id), headers=internal_headers)
    assert response.status_code == 200


def test_analytics_endpoint_project_not_found(client, internal_headers):
    response = client.get(_url(uuid.uuid4()), headers=internal_headers)
    assert response.status_code == 404


def test_analytics_endpoint_returns_expected_shape(
    client, internal_headers, project_factory, article_factory, classification_factory
):
    project = project_factory()
    article = article_factory(project, count=1, retailer="Auchan", audience=5000.0)[0]
    classification_factory(article, primary_topic="store_expansion", sentiment="positive")

    response = client.get(_url(project.id), headers=internal_headers)
    body = response.json()

    assert body["project_id"] == str(project.id)
    assert set(body.keys()) == {
        "project_id",
        "filters",
        "available_filter_options",
        "top_n",
        "kpis",
        "brands",
        "topics",
        "sentiment",
        "publications_and_stories",
    }
    assert body["kpis"]["unique_valid_articles"] == 1
    assert body["brands"]["by_volume"][0]["brand"] == "Auchan"


def test_analytics_endpoint_top_n_default(client, internal_headers, project_factory):
    project = project_factory()
    response = client.get(_url(project.id), headers=internal_headers)
    assert response.json()["top_n"] == 10


def test_analytics_endpoint_top_n_minimum_boundary(client, internal_headers, project_factory):
    project = project_factory()
    response = client.get(_url(project.id, top_n=1), headers=internal_headers)
    assert response.status_code == 200
    assert response.json()["top_n"] == 1


def test_analytics_endpoint_top_n_maximum_boundary(client, internal_headers, project_factory):
    project = project_factory()
    response = client.get(_url(project.id, top_n=50), headers=internal_headers)
    assert response.status_code == 200
    assert response.json()["top_n"] == 50


def test_analytics_endpoint_top_n_below_minimum_rejected(client, internal_headers, project_factory):
    project = project_factory()
    response = client.get(_url(project.id, top_n=0), headers=internal_headers)
    assert response.status_code == 422


def test_analytics_endpoint_top_n_above_maximum_rejected(client, internal_headers, project_factory):
    project = project_factory()
    response = client.get(_url(project.id, top_n=51), headers=internal_headers)
    assert response.status_code == 422


def test_analytics_endpoint_applies_filter_query_params(
    client, internal_headers, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=2, retailer="Auchan")
    article_factory(project, count=3, retailer="Carrefour")

    response = client.get(_url(project.id, brand="Auchan"), headers=internal_headers)
    body = response.json()

    assert body["filters"]["brand"] == "Auchan"
    assert body["kpis"]["unique_valid_articles"] == 2
