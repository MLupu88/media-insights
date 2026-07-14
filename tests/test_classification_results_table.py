from datetime import date


def _results_url(project_id, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/projects/{project_id}?tab=classification"
    return f"{url}&{query}" if query else url


def test_classification_tab_displays_saved_results(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 1
    project.classified_rows = 1
    db_session.commit()
    article = article_factory(project, count=1, title="Auchan opens new hypermarket", source="Ziarul Financiar")[0]
    classification_factory(
        article, primary_topic="store_expansion", sentiment="positive", brand_role="primary_focus"
    )

    response = authenticated_client.get(_results_url(project.id))

    assert response.status_code == 200
    assert "Auchan opens new hypermarket" in response.text
    assert "Ziarul Financiar" in response.text
    assert "Store expansion" in response.text
    assert "Positive" in response.text
    assert "Primary focus" in response.text


def test_unauthenticated_redirects_to_login(client, project_factory):
    project = project_factory()

    response = client.get(f"/projects/{project.id}?tab=classification", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_cross_project_isolation(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project_a = project_factory(name="Results Project A")
    project_b = project_factory(name="Results Project B")
    for p in (project_a, project_b):
        p.valid_rows = 1
        p.classified_rows = 1
    db_session.commit()

    article_a = article_factory(project_a, count=1, title="Belongs to A")[0]
    article_b = article_factory(project_b, count=1, title="Belongs to B")[0]
    classification_factory(article_a)
    classification_factory(article_b)

    response = authenticated_client.get(_results_url(project_a.id))

    assert response.status_code == 200
    assert "Belongs to A" in response.text
    assert "Belongs to B" not in response.text


def test_article_url_is_linked_correctly(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 2
    project.classified_rows = 2
    db_session.commit()
    with_url = article_factory(
        project, count=1, title="Has a URL", article_url="https://example.test/article-1"
    )[0]
    without_url = article_factory(project, count=1, title="No URL here")[0]
    classification_factory(with_url)
    classification_factory(without_url)

    response = authenticated_client.get(_results_url(project.id))

    assert response.status_code == 200
    assert '<a href="https://example.test/article-1"' in response.text
    # The row without a URL must render the title as plain text, not a
    # link pointing nowhere.
    no_url_index = response.text.index("No URL here")
    preceding = response.text[max(0, no_url_index - 300) : no_url_index]
    assert "<a href=\"\"" not in preceding


def test_filter_by_primary_topic(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 2
    project.classified_rows = 2
    db_session.commit()
    expansion_article = article_factory(project, count=1, title="Expansion Story")[0]
    pricing_article = article_factory(project, count=1, title="Pricing Story")[0]
    classification_factory(expansion_article, primary_topic="store_expansion")
    classification_factory(pricing_article, primary_topic="promotions_pricing")

    response = authenticated_client.get(_results_url(project.id, results_primary_topic="store_expansion"))

    assert response.status_code == 200
    assert "Expansion Story" in response.text
    assert "Pricing Story" not in response.text


def test_filter_by_sentiment(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 2
    project.classified_rows = 2
    db_session.commit()
    positive_article = article_factory(project, count=1, title="Positive Story")[0]
    negative_article = article_factory(project, count=1, title="Negative Story")[0]
    classification_factory(positive_article, sentiment="positive")
    classification_factory(negative_article, sentiment="negative")

    response = authenticated_client.get(_results_url(project.id, results_sentiment="negative"))

    assert response.status_code == 200
    assert "Negative Story" in response.text
    assert "Positive Story" not in response.text


def test_filter_by_confidence_bucket(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 2
    project.classified_rows = 2
    db_session.commit()
    low_article = article_factory(project, count=1, title="Low Confidence Story")[0]
    high_article = article_factory(project, count=1, title="High Confidence Story")[0]
    classification_factory(low_article, confidence=0.3)
    classification_factory(high_article, confidence=0.95)

    low_response = authenticated_client.get(_results_url(project.id, results_confidence="low"))
    high_response = authenticated_client.get(_results_url(project.id, results_confidence="high"))

    assert "Low Confidence Story" in low_response.text
    assert "High Confidence Story" not in low_response.text
    assert "High Confidence Story" in high_response.text
    assert "Low Confidence Story" not in high_response.text


def test_invalid_filter_value_is_rejected(authenticated_client, project_factory):
    project = project_factory()

    response = authenticated_client.get(
        _results_url(project.id, results_primary_topic="not_a_real_topic")
    )

    assert response.status_code == 400


def test_search_matches_title_subject_publication_and_story_key(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 3
    project.classified_rows = 3
    db_session.commit()
    title_match = article_factory(project, count=1, title="Kaufland Brasov opening", source="Source A")[0]
    story_key_match = article_factory(project, count=1, title="Unrelated headline", source="Source B")[0]
    no_match = article_factory(project, count=1, title="Something else", source="Source C")[0]
    classification_factory(title_match)
    classification_factory(story_key_match, story_key="Kaufland Brasov expansion")
    classification_factory(no_match)

    response = authenticated_client.get(_results_url(project.id, results_search="Kaufland"))

    assert response.status_code == 200
    assert "Kaufland Brasov opening" in response.text
    assert "Unrelated headline" in response.text
    assert "Something else" not in response.text


def test_sort_by_reach_descending(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 2
    project.classified_rows = 2
    db_session.commit()
    low_reach = article_factory(project, count=1, title="Low Reach Article", audience=1000.0)[0]
    high_reach = article_factory(project, count=1, title="High Reach Article", audience=500000.0)[0]
    classification_factory(low_reach)
    classification_factory(high_reach)

    response = authenticated_client.get(_results_url(project.id, results_sort="reach_desc"))

    assert response.status_code == 200
    assert response.text.index("High Reach Article") < response.text.index("Low Reach Article")


def test_sort_by_confidence_ascending(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 2
    project.classified_rows = 2
    db_session.commit()
    low_conf = article_factory(project, count=1, title="Low Conf Article")[0]
    high_conf = article_factory(project, count=1, title="High Conf Article")[0]
    classification_factory(low_conf, confidence=0.2)
    classification_factory(high_conf, confidence=0.9)

    response = authenticated_client.get(_results_url(project.id, results_sort="confidence_asc"))

    assert response.status_code == 200
    assert response.text.index("Low Conf Article") < response.text.index("High Conf Article")


def test_default_sort_is_publication_date_descending(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 2
    project.classified_rows = 2
    db_session.commit()
    older = article_factory(project, count=1, title="Older Article", publication_date=date(2026, 1, 1))[0]
    newer = article_factory(project, count=1, title="Newer Article", publication_date=date(2026, 6, 1))[0]
    classification_factory(older)
    classification_factory(newer)

    response = authenticated_client.get(_results_url(project.id))

    assert response.status_code == 200
    assert response.text.index("Newer Article") < response.text.index("Older Article")


def test_pagination_limits_rows_per_page_and_navigates(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 3
    project.classified_rows = 3
    db_session.commit()
    articles = article_factory(project, count=3)
    for i, article in enumerate(articles):
        article.title = f"Paginated Article {i}"
        classification_factory(article)
    db_session.commit()

    first_page = authenticated_client.get(_results_url(project.id, results_page_size=1, results_page=1))
    second_page = authenticated_client.get(_results_url(project.id, results_page_size=1, results_page=2))

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert "Page 1 of 3" in first_page.text
    assert "Page 2 of 3" in second_page.text
    # The two pages must not show the same article.
    first_titles = {f"Paginated Article {i}" for i in range(3) if f"Paginated Article {i}" in first_page.text}
    second_titles = {f"Paginated Article {i}" for i in range(3) if f"Paginated Article {i}" in second_page.text}
    assert first_titles.isdisjoint(second_titles)


def test_default_page_size_is_fifty(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    project.valid_rows = 3
    project.classified_rows = 3
    db_session.commit()
    for article in article_factory(project, count=3):
        classification_factory(article)

    response = authenticated_client.get(_results_url(project.id))

    assert response.status_code == 200
    assert "Page 1 of 1" in response.text
