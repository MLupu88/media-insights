def test_compare_page_shows_pickers_with_only_one_project(authenticated_client, project_factory):
    """A single project is enough to compare -- Phase E allows selecting
    the same project on both sides for a same-project brand-vs-brand
    comparison, so the "not enough projects" empty state no longer
    applies until there are zero projects at all.
    """
    project_factory(name="Only One", quarter="2026-Q1")

    response = authenticated_client.get("/compare")

    assert response.status_code == 200
    assert "Not enough projects to compare" not in response.text
    assert 'name="baseline_project_ids"' in response.text
    assert 'name="comparison_project_ids"' in response.text


def test_compare_page_empty_state_with_zero_projects(authenticated_client):
    response = authenticated_client.get("/compare")

    assert response.status_code == 200
    assert "No projects to compare" in response.text


def test_compare_page_shows_pickers_with_two_or_more_projects(
    authenticated_client, project_factory
):
    project_factory(name="Project A", quarter="2026-Q1")
    project_factory(name="Project B", quarter="2026-Q2")

    response = authenticated_client.get("/compare")

    assert response.status_code == 200
    assert "No comparison yet" in response.text
    assert "Project A" in response.text
    assert "Project B" in response.text
    assert 'id="baseline_project_ids"' in response.text
    assert 'id="comparison_project_ids"' in response.text


def test_compare_page_renders_full_comparison(
    authenticated_client, project_factory, article_factory
):
    a = project_factory(name="Project A", quarter="2026-Q1")
    b = project_factory(name="Project B", quarter="2026-Q2")
    article_factory(a, count=2, retailer="Auchan", audience=1000.0)
    article_factory(b, count=3, retailer="Auchan", audience=2000.0)

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={a.id}&comparison_project_ids={b.id}"
    )

    assert response.status_code == 200
    assert "Q1 2026" in response.text
    assert "Q2 2026" in response.text
    assert "Coverage volume" in response.text
    assert "Share of Voice by brand" in response.text
    assert "Publication movement" in response.text
    assert "Volatility between periods" in response.text


def test_compare_page_h1_derivation_label(authenticated_client, project_factory, article_factory):
    q1 = project_factory(name="Q1", quarter="2026-Q1")
    q2 = project_factory(name="Q2", quarter="2026-Q2")
    q3 = project_factory(name="Q3", quarter="2026-Q3")
    article_factory(q1, count=1, retailer="Auchan")
    article_factory(q2, count=1, retailer="Auchan")
    article_factory(q3, count=1, retailer="Auchan")

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={q1.id}&baseline_project_ids={q2.id}"
        f"&comparison_project_ids={q3.id}"
    )

    assert response.status_code == 200
    assert "H1 2026" in response.text


def test_compare_page_requires_authentication(client, project_factory):
    project_factory(name="A", quarter="2026-Q1")
    project_factory(name="B", quarter="2026-Q2")

    response = client.get("/compare", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_compare_page_does_not_leak_internal_secret(
    authenticated_client, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={a.id}&comparison_project_ids={b.id}"
    )

    assert "test-internal-secret" not in response.text


def test_compare_page_and_internal_api_report_matching_figures(
    authenticated_client, internal_headers, project_factory, article_factory, classification_factory
):
    """API/UI consistency: both surfaces call the same shared comparison
    service, so the numbers they show for the same selection must match.
    """
    a = project_factory(name="Project A", quarter="2026-Q1")
    b = project_factory(name="Project B", quarter="2026-Q2")
    articles_a = article_factory(a, count=3, retailer="Auchan", audience=1500.0)
    articles_b = article_factory(b, count=2, retailer="Auchan", audience=3000.0)
    classification_factory(articles_a[0], primary_topic="store_expansion", sentiment="positive")
    classification_factory(articles_b[0], primary_topic="promotions_pricing", sentiment="negative")

    ui_response = authenticated_client.get(
        f"/compare?baseline_project_ids={a.id}&comparison_project_ids={b.id}"
    )
    api_response = authenticated_client.get(
        f"/api/internal/compare?baseline_project_ids={a.id}&comparison_project_ids={b.id}",
        headers=internal_headers,
    )

    assert ui_response.status_code == 200
    assert api_response.status_code == 200
    api_body = api_response.json()

    assert api_body["baseline"]["label"] in ui_response.text
    assert api_body["comparison"]["label"] in ui_response.text

    # The API schema types KPI deltas as float (e.g. 3.0); the UI renders the
    # underlying Python int directly (3). Compare as numbers, not raw text,
    # so this isn't a false mismatch between two valid representations of
    # the same value.
    baseline_count = api_body["deltas"]["kpis"]["unique_valid_articles"]["baseline"]
    comparison_count = api_body["deltas"]["kpis"]["unique_valid_articles"]["comparison"]
    assert str(int(baseline_count)) in ui_response.text
    assert str(int(comparison_count)) in ui_response.text

    for brand_row in api_body["deltas"]["brands"]:
        assert brand_row["brand"] in ui_response.text


def test_compare_page_filter_options_stable_across_filtered_request(
    authenticated_client, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan", source="Ziarul")
    article_factory(b, count=1, retailer="Carrefour", source="Adevarul")

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={a.id}&comparison_project_ids={b.id}&brand=Auchan"
    )

    assert response.status_code == 200
    # Both publications must still be listed even though we filtered by brand.
    assert "Ziarul" in response.text
    assert "Adevarul" in response.text


# ---------------------------------------------------------------------------
# Phase E: same-project brand-vs-brand comparison, wired through the UI
# ---------------------------------------------------------------------------


def test_compare_page_accepts_same_project_on_both_sides(
    authenticated_client, project_factory, article_factory
):
    project = project_factory(name="Same Project")
    article_factory(project, count=3, retailer="Auchan")
    article_factory(project, count=2, retailer="Carrefour")

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={project.id}&comparison_project_ids={project.id}"
        "&baseline_filter_brand=Auchan&comparison_filter_brand=Carrefour"
    )

    assert response.status_code == 200
    assert "Auchan" in response.text
    assert "Carrefour" in response.text
    # Independent per-side KPI figures, not a shared/blended population.
    assert "3" in response.text
    assert "2" in response.text


def test_compare_page_brand_checkboxes_are_independently_pre_checked_per_side(
    authenticated_client, project_factory, article_factory
):
    project = project_factory(name="Checkbox Project")
    article_factory(project, count=1, retailer="Auchan")
    article_factory(project, count=1, retailer="Carrefour")

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={project.id}&comparison_project_ids={project.id}"
        "&baseline_filter_brand=Auchan&comparison_filter_brand=Carrefour"
    )

    assert response.status_code == 200
    assert 'name="baseline_filter_brand" value="Auchan" checked' in response.text
    assert 'name="comparison_filter_brand" value="Carrefour" checked' in response.text
    # Each side's checkbox for the OTHER side's brand must not be checked.
    assert 'name="baseline_filter_brand" value="Carrefour" checked' not in response.text
    assert 'name="comparison_filter_brand" value="Auchan" checked' not in response.text


def test_compare_page_labels_are_brand_based_for_same_project_comparison(
    authenticated_client, project_factory, article_factory
):
    project = project_factory(name="Label Project", quarter="2026-Q2")
    article_factory(project, count=1, retailer="Auchan")
    article_factory(project, count=1, retailer="Carrefour")

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={project.id}&comparison_project_ids={project.id}"
        "&baseline_filter_brand=Auchan&comparison_filter_brand=Carrefour"
    )

    assert response.status_code == 200
    # Brand-based labels, not the quarter-collision "Q2 2026 vs Q2 2026".
    assert ">Auchan<" in response.text
    assert ">Carrefour<" in response.text


def test_compare_page_export_links_carry_independent_side_filters(
    authenticated_client, project_factory, article_factory
):
    project = project_factory(name="Export Filter Project")
    article_factory(project, count=1, retailer="Auchan")
    article_factory(project, count=1, retailer="Carrefour")

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={project.id}&comparison_project_ids={project.id}"
        "&baseline_filter_brand=Auchan&comparison_filter_brand=Carrefour"
    )

    assert response.status_code == 200
    assert "baseline_filter_brands=Auchan" in response.text
    assert "comparison_filter_brands=Carrefour" in response.text
    assert "report.pptx?" in response.text
    assert "report.xlsx?" in response.text


def test_compare_page_no_double_counting_for_same_project_disjoint_brands(
    authenticated_client, internal_headers, project_factory, article_factory
):
    project = project_factory(name="No Double Count")
    article_factory(project, count=4, retailer="Auchan")
    article_factory(project, count=3, retailer="Carrefour")

    ui_response = authenticated_client.get(
        f"/compare?baseline_project_ids={project.id}&comparison_project_ids={project.id}"
        "&baseline_filter_brand=Auchan&comparison_filter_brand=Carrefour"
    )
    api_response = authenticated_client.get(
        f"/api/internal/compare?baseline_project_ids={project.id}&comparison_project_ids={project.id}"
        "&baseline_filter_brand=Auchan&comparison_filter_brand=Carrefour",
        headers=internal_headers,
    )

    assert ui_response.status_code == 200
    assert api_response.status_code == 200
    api_body = api_response.json()
    # Baseline (4 Auchan) and comparison (3 Carrefour) never blend or sum
    # into a combined 7 -- each side reports its own independent count.
    assert api_body["baseline"]["kpis"]["unique_valid_articles"] == 4
    assert api_body["comparison"]["kpis"]["unique_valid_articles"] == 3
