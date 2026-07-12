import uuid

from app.models.classification import Classification, ClassificationBatch, ClassificationBatchArticle

BULK_URL = "/api/internal/classifications/bulk"


def _make_batch(db_session, project, articles):
    batch = ClassificationBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        status="pending",
        article_count=len(articles),
    )
    db_session.add(batch)
    db_session.add_all(
        ClassificationBatchArticle(batch_id=batch.id, article_id=article.id)
        for article in articles
    )
    db_session.commit()
    db_session.refresh(batch)
    return batch


def _valid_result(article_id, **overrides):
    result = {
        "article_id": str(article_id),
        "primary_topic": "store_expansion",
        "secondary_topic": "investment_operations",
        "communication_category": "corporate",
        "sentiment": "positive",
        "brand_role": "primary_focus",
        "story_key": "Kaufland deschide magazin Brasov",
        "confidence": 0.95,
        "rationale_ro": "Titlul anunta deschiderea unui nou magazin.",
    }
    result.update(overrides)
    return result


def test_bulk_classification_requires_internal_secret(
    client, project_factory, article_factory, db_session
):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, articles)

    response = client.post(
        BULK_URL,
        json={
            "project_id": str(project.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "results": [_valid_result(articles[0].id)],
        },
    )

    assert response.status_code == 401


def test_bulk_classification_insert(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, articles)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "results": [_valid_result(articles[0].id)],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "saved", "saved_count": 1, "updated_count": 0, "rejected_count": 0}

    classification = (
        db_session.query(Classification).filter_by(article_id=articles[0].id).one()
    )
    assert classification.primary_topic == "store_expansion"
    assert classification.confidence == 0.95


def test_bulk_classification_upsert_updates_existing(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, articles)

    payload = {
        "project_id": str(project.id),
        "batch_id": str(batch.id),
        "model": "deepseek-chat",
        "prompt_version": "retail-deepseek-v2",
        "results": [_valid_result(articles[0].id)],
    }
    first = client.post(BULK_URL, headers=internal_headers, json=payload)
    assert first.json()["saved_count"] == 1

    payload["results"][0]["primary_topic"] = "crisis_controversy"
    payload["results"][0]["confidence"] = 0.42
    second = client.post(BULK_URL, headers=internal_headers, json=payload)

    assert second.status_code == 200
    assert second.json() == {
        "status": "saved",
        "saved_count": 0,
        "updated_count": 1,
        "rejected_count": 0,
    }

    assert db_session.query(Classification).filter_by(article_id=articles[0].id).count() == 1
    classification = (
        db_session.query(Classification).filter_by(article_id=articles[0].id).one()
    )
    assert classification.primary_topic == "crisis_controversy"
    assert classification.confidence == 0.42


def test_bulk_classification_recomputes_project_classified_rows(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=2)
    batch = _make_batch(db_session, project, articles)

    client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "results": [_valid_result(a.id) for a in articles],
        },
    )

    db_session.refresh(project)
    assert project.classified_rows == 2


def test_bulk_classification_rejects_invalid_taxonomy_value(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, articles)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "results": [_valid_result(articles[0].id, primary_topic="not_a_real_topic")],
        },
    )

    assert response.status_code == 422
    assert db_session.query(Classification).count() == 0


def test_bulk_classification_rejects_out_of_range_confidence(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, articles)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "results": [_valid_result(articles[0].id, confidence=1.5)],
        },
    )

    assert response.status_code == 422
    assert db_session.query(Classification).count() == 0


def test_bulk_classification_rejects_duplicate_article_id_in_request(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, articles)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "partial_save": True,
            "results": [_valid_result(articles[0].id), _valid_result(articles[0].id)],
        },
    )

    assert response.status_code == 422
    assert "duplicate" in response.json()["detail"].lower()


def test_bulk_classification_rejects_article_not_in_project(
    client, internal_headers, db_session, project_factory, article_factory
):
    project_a = project_factory(name="Project A")
    project_b = project_factory(name="Project B")
    articles_a = article_factory(project_a, count=1)
    articles_b = article_factory(project_b, count=1)
    batch = _make_batch(db_session, project_a, articles_a)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project_a.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "partial_save": True,
            "results": [_valid_result(articles_b[0].id)],
        },
    )

    assert response.status_code == 422
    assert db_session.query(Classification).count() == 0


def test_bulk_classification_rejects_article_not_in_batch(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    batched_articles = article_factory(project, count=1)
    unbatched_articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, batched_articles)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "partial_save": True,
            "results": [_valid_result(unbatched_articles[0].id)],
        },
    )

    assert response.status_code == 422
    assert db_session.query(Classification).count() == 0


def test_bulk_classification_batch_not_found(
    client, internal_headers, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=1)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(uuid.uuid4()),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "results": [_valid_result(articles[0].id)],
        },
    )

    assert response.status_code == 404


def test_bulk_classification_batch_project_mismatch(
    client, internal_headers, db_session, project_factory, article_factory
):
    project_a = project_factory(name="Project A")
    project_b = project_factory(name="Project B")
    articles_a = article_factory(project_a, count=1)
    batch = _make_batch(db_session, project_a, articles_a)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project_b.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "results": [_valid_result(articles_a[0].id)],
        },
    )

    assert response.status_code == 422


def test_bulk_classification_result_count_mismatch_rejected_without_partial_save(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=3)
    batch = _make_batch(db_session, project, articles)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "results": [_valid_result(articles[0].id)],
        },
    )

    assert response.status_code == 422
    assert db_session.query(Classification).count() == 0


def test_bulk_classification_partial_save_allows_subset(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=3)
    batch = _make_batch(db_session, project, articles)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(batch.id),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "partial_save": True,
            "results": [_valid_result(articles[0].id)],
        },
    )

    assert response.status_code == 200
    assert response.json()["saved_count"] == 1
    assert db_session.query(Classification).count() == 1


def test_bulk_classification_malformed_result_rejected_with_clear_error(
    client, internal_headers, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=1)

    response = client.post(
        BULK_URL,
        headers=internal_headers,
        json={
            "project_id": str(project.id),
            "batch_id": str(uuid.uuid4()),
            "model": "deepseek-chat",
            "prompt_version": "retail-deepseek-v2",
            "results": [{"article_id": str(articles[0].id)}],  # missing required fields
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert len(detail) > 0
