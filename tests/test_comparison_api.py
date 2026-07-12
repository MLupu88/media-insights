import uuid


def _url(baseline_ids, comparison_ids, **extra) -> str:
    params = []
    for pid in baseline_ids:
        params.append(f"baseline_project_ids={pid}")
    for pid in comparison_ids:
        params.append(f"comparison_project_ids={pid}")
    for key, value in extra.items():
        params.append(f"{key}={value}")
    return "/api/internal/compare?" + "&".join(params)


def test_compare_endpoint_requires_internal_secret(client, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    response = client.get(_url([a.id], [b.id]))
    assert response.status_code == 401


def test_compare_endpoint_rejects_invalid_secret(client, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    response = client.get(_url([a.id], [b.id]), headers={"x-internal-secret": "wrong"})
    assert response.status_code == 401


def test_compare_endpoint_accepts_valid_secret(client, internal_headers, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    response = client.get(_url([a.id], [b.id]), headers=internal_headers)
    assert response.status_code == 200


def test_compare_endpoint_project_not_found(client, internal_headers, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    response = client.get(_url([a.id], [uuid.uuid4()]), headers=internal_headers)
    assert response.status_code == 404


def test_compare_endpoint_empty_baseline_rejected(client, internal_headers, project_factory):
    b = project_factory(name="B", quarter="2026-Q2")
    response = client.get(f"/api/internal/compare?comparison_project_ids={b.id}", headers=internal_headers)
    assert response.status_code == 422


def test_compare_endpoint_empty_comparison_rejected(client, internal_headers, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    response = client.get(f"/api/internal/compare?baseline_project_ids={a.id}", headers=internal_headers)
    assert response.status_code == 422


def test_compare_endpoint_returns_expected_shape(
    client, internal_headers, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=2, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    response = client.get(_url([a.id], [b.id]), headers=internal_headers)
    body = response.json()

    assert set(body.keys()) == {
        "baseline",
        "comparison",
        "available_filter_options",
        "top_n",
        "deltas",
        "volatility",
    }
    assert body["baseline"]["label"] == "Q1 2026"
    assert body["comparison"]["label"] == "Q2 2026"
    assert body["deltas"]["kpis"]["unique_valid_articles"]["baseline"] == 2
    assert body["deltas"]["kpis"]["unique_valid_articles"]["comparison"] == 1


def test_compare_endpoint_multi_project_baseline(
    client, internal_headers, project_factory, article_factory
):
    q1 = project_factory(name="Q1", quarter="2026-Q1")
    q2 = project_factory(name="Q2", quarter="2026-Q2")
    q3 = project_factory(name="Q3", quarter="2026-Q3")
    article_factory(q1, count=1, retailer="Auchan")
    article_factory(q2, count=1, retailer="Auchan")
    article_factory(q3, count=1, retailer="Auchan")

    response = client.get(_url([q1.id, q2.id], [q3.id]), headers=internal_headers)
    body = response.json()

    assert body["baseline"]["label"] == "H1 2026"
    assert body["baseline"]["kpis"]["unique_valid_articles"] == 2


def test_compare_endpoint_top_n_default(client, internal_headers, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    response = client.get(_url([a.id], [b.id]), headers=internal_headers)
    assert response.json()["top_n"] == 10


def test_compare_endpoint_top_n_bounds(client, internal_headers, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")

    ok_min = client.get(_url([a.id], [b.id], top_n=1), headers=internal_headers)
    ok_max = client.get(_url([a.id], [b.id], top_n=50), headers=internal_headers)
    too_low = client.get(_url([a.id], [b.id], top_n=0), headers=internal_headers)
    too_high = client.get(_url([a.id], [b.id], top_n=51), headers=internal_headers)

    assert ok_min.status_code == 200
    assert ok_max.status_code == 200
    assert too_low.status_code == 422
    assert too_high.status_code == 422


def test_compare_endpoint_applies_filters(
    client, internal_headers, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=2, retailer="Auchan")
    article_factory(a, count=3, retailer="Carrefour")
    article_factory(b, count=1, retailer="Auchan")

    response = client.get(_url([a.id], [b.id], brand="Auchan"), headers=internal_headers)
    body = response.json()

    assert body["baseline"]["filters"]["brand"] == "Auchan"
    assert body["baseline"]["kpis"]["unique_valid_articles"] == 2
