import uuid

from app.models.article import ImportStatus
from app.models.classification import (
    Classification,
    ClassificationBatch,
    ClassificationBatchArticle,
    ClassificationBatchStatus,
)


def _batches_url(project_id, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/internal/projects/{project_id}/classification-batches"
    return f"{url}?{query}" if query else url


def test_batch_generation_creates_batches_for_valid_articles(
    client, internal_headers, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=5)

    response = client.get(
        _batches_url(project.id, batch_size=2), headers=internal_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == str(project.id)
    assert len(body["batches"]) == 3  # 2 + 2 + 1
    total_articles = sum(len(b["articles"]) for b in body["batches"])
    assert total_articles == 5


def test_batch_generation_does_not_create_empty_batches(
    client, internal_headers, project_factory
):
    project = project_factory()

    response = client.get(_batches_url(project.id), headers=internal_headers)

    assert response.status_code == 200
    assert response.json()["batches"] == []


def test_batch_generation_ordering_is_deterministic(db_session, project_factory, article_factory):
    from app.services.classification import _eligible_articles

    project = project_factory()
    article_factory(project, count=6)

    first_order = [a.id for a in _eligible_articles(db_session, project.id, True)]
    second_order = [a.id for a in _eligible_articles(db_session, project.id, True)]

    assert first_order == second_order
    assert len(first_order) == 6


def test_batch_size_minimum_boundary(client, internal_headers, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=3)

    response = client.get(_batches_url(project.id, batch_size=1), headers=internal_headers)

    assert response.status_code == 200
    assert len(response.json()["batches"]) == 3


def test_batch_size_maximum_boundary(client, internal_headers, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=3)

    response = client.get(_batches_url(project.id, batch_size=100), headers=internal_headers)

    assert response.status_code == 200
    assert len(response.json()["batches"]) == 1


def test_batch_size_zero_is_rejected(client, internal_headers, project_factory):
    project = project_factory()

    response = client.get(_batches_url(project.id, batch_size=0), headers=internal_headers)

    assert response.status_code == 422


def test_batch_size_above_maximum_is_rejected(client, internal_headers, project_factory):
    project = project_factory()

    response = client.get(_batches_url(project.id, batch_size=101), headers=internal_headers)

    assert response.status_code == 422


def test_batch_generation_project_not_found(client, internal_headers):
    response = client.get(_batches_url(uuid.uuid4()), headers=internal_headers)
    assert response.status_code == 404


def test_only_unclassified_true_excludes_already_classified_articles(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=3)

    db_session.add(
        Classification(
            id=uuid.uuid4(),
            article_id=articles[0].id,
            project_id=project.id,
            primary_topic="other",
            communication_category="incidental",
            sentiment="neutral",
            brand_role="incidental_mention",
            confidence=0.9,
            model="deepseek-chat",
            prompt_version="retail-deepseek-v2",
        )
    )
    db_session.commit()

    response = client.get(
        _batches_url(project.id, batch_size=100, only_unclassified="true"),
        headers=internal_headers,
    )

    body = response.json()
    returned_ids = {a["article_id"] for b in body["batches"] for a in b["articles"]}
    assert str(articles[0].id) not in returned_ids
    assert str(articles[1].id) in returned_ids
    assert str(articles[2].id) in returned_ids


def test_only_unclassified_false_includes_already_classified_articles(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=3)

    db_session.add(
        Classification(
            id=uuid.uuid4(),
            article_id=articles[0].id,
            project_id=project.id,
            primary_topic="other",
            communication_category="incidental",
            sentiment="neutral",
            brand_role="incidental_mention",
            confidence=0.9,
            model="deepseek-chat",
            prompt_version="retail-deepseek-v2",
        )
    )
    db_session.commit()

    response = client.get(
        _batches_url(project.id, batch_size=100, only_unclassified="false"),
        headers=internal_headers,
    )

    body = response.json()
    returned_ids = {a["article_id"] for b in body["batches"] for a in b["articles"]}
    assert str(articles[0].id) in returned_ids


def test_invalid_rows_are_excluded_from_batches(
    client, internal_headers, project_factory, article_factory
):
    project = project_factory()
    valid_articles = article_factory(project, count=2, import_status=ImportStatus.VALID)
    invalid_articles = article_factory(project, count=2, import_status=ImportStatus.INVALID)

    response = client.get(
        _batches_url(project.id, batch_size=100), headers=internal_headers
    )

    body = response.json()
    returned_ids = {a["article_id"] for b in body["batches"] for a in b["articles"]}
    for article in valid_articles:
        assert str(article.id) in returned_ids
    for article in invalid_articles:
        assert str(article.id) not in returned_ids


def test_duplicate_rows_are_preserved_and_flagged_in_payload(
    client, internal_headers, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, is_duplicate=False)
    article_factory(project, count=1, is_duplicate=True)

    response = client.get(
        _batches_url(project.id, batch_size=100), headers=internal_headers
    )

    body = response.json()
    articles = [a for b in body["batches"] for a in b["articles"]]
    assert len(articles) == 2
    duplicate_flags = {a["is_duplicate"] for a in articles}
    assert duplicate_flags == {True, False}


def test_article_not_assigned_to_multiple_active_batches(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=5)

    first_response = client.get(
        _batches_url(project.id, batch_size=2), headers=internal_headers
    )
    first_batches = first_response.json()["batches"]
    assigned_ids = {a["article_id"] for b in first_batches for a in b["articles"]}
    assert len(assigned_ids) == 5

    # All 5 articles are now claimed by active (pending) batches; a second
    # generation call must find nothing left to assign.
    second_response = client.get(
        _batches_url(project.id, batch_size=2), headers=internal_headers
    )
    assert second_response.json()["batches"] == []

    # Sanity: the batch/article link rows exist and are unique per article.
    links = db_session.query(ClassificationBatchArticle).all()
    article_ids = [link.article_id for link in links]
    assert len(article_ids) == len(set(article_ids)) == 5


def test_completed_batch_frees_its_articles_from_active_exclusion(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=2)

    first = client.get(_batches_url(project.id, batch_size=2), headers=internal_headers).json()
    batch_id = first["batches"][0]["batch_id"]

    batch = db_session.get(ClassificationBatch, uuid.UUID(batch_id))
    batch.status = ClassificationBatchStatus.FAILED
    db_session.commit()

    # A failed (non-active) batch should not block re-generation for those articles.
    second = client.get(
        _batches_url(project.id, batch_size=2, only_unclassified="false"),
        headers=internal_headers,
    ).json()
    assert len(second["batches"]) == 1
    assert len(second["batches"][0]["articles"]) == 2


def test_batch_payload_uses_source_as_publication_and_audience_as_reach(
    client, internal_headers, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, source="Ziarul Financiar", audience=250000.0)

    response = client.get(_batches_url(project.id, batch_size=10), headers=internal_headers)

    article = response.json()["batches"][0]["articles"][0]
    assert article["publication"] == "Ziarul Financiar"
    assert article["reach"] == 250000.0
