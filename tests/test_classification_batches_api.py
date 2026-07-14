import threading
import uuid

from app.models.article import ImportStatus
from app.models.classification import (
    Classification,
    ClassificationBatch,
    ClassificationBatchArticle,
    ClassificationBatchStatus,
)
from app.database import SessionLocal


def _batches_url(project_id, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/internal/projects/{project_id}/classification-batches"
    return f"{url}?{query}" if query else url


def test_batch_generation_creates_exactly_one_batch_per_request(
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
    assert body["already_running"] is False
    assert len(body["batches"]) == 1
    assert len(body["batches"][0]["articles"]) == 2

    batch_id = uuid.UUID(body["batches"][0]["batch_id"])
    with SessionLocal() as session:
        batch = session.get(ClassificationBatch, batch_id)
        assert batch.status == ClassificationBatchStatus.RUNNING


def test_batch_generation_does_not_create_empty_batches(
    client, internal_headers, project_factory
):
    project = project_factory()

    response = client.get(_batches_url(project.id), headers=internal_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["batches"] == []
    assert body["already_running"] is False


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
    body = response.json()
    assert len(body["batches"]) == 1
    assert len(body["batches"][0]["articles"]) == 1


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


def test_batch_payload_uses_source_as_publication_and_audience_as_reach(
    client, internal_headers, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, source="Ziarul Financiar", audience=250000.0)

    response = client.get(_batches_url(project.id, batch_size=10), headers=internal_headers)

    article = response.json()["batches"][0]["articles"][0]
    assert article["publication"] == "Ziarul Financiar"
    assert article["reach"] == 250000.0


# --- one batch per request / active-batch reuse (requirements 1-2, 7) ------


def test_second_call_while_batch_running_returns_no_new_batch(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=5)

    first = client.get(_batches_url(project.id, batch_size=2), headers=internal_headers).json()
    assert len(first["batches"]) == 1
    assert first["already_running"] is False

    second = client.get(_batches_url(project.id, batch_size=2), headers=internal_headers).json()
    assert second["batches"] == []
    assert second["already_running"] is True

    # Only one batch, and only the first call's 2 articles, were ever claimed.
    assert db_session.query(ClassificationBatch).filter_by(project_id=project.id).count() == 1
    links = (
        db_session.query(ClassificationBatchArticle)
        .join(ClassificationBatch)
        .filter(ClassificationBatch.project_id == project.id)
        .all()
    )
    assert len(links) == 2


def test_pending_batch_is_claimed_and_returned_once(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=2)
    batch = ClassificationBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        status=ClassificationBatchStatus.PENDING,
        article_count=2,
    )
    db_session.add(batch)
    db_session.add_all(
        ClassificationBatchArticle(batch_id=batch.id, article_id=a.id) for a in articles
    )
    db_session.commit()

    response = client.get(_batches_url(project.id, batch_size=40), headers=internal_headers)
    body = response.json()

    assert body["already_running"] is False
    assert len(body["batches"]) == 1
    assert body["batches"][0]["batch_id"] == str(batch.id)

    db_session.rollback()
    db_session.refresh(batch)
    assert batch.status == ClassificationBatchStatus.RUNNING

    # A second call now sees it as running, not pending -- no duplicate.
    second = client.get(_batches_url(project.id, batch_size=40), headers=internal_headers).json()
    assert second["batches"] == []
    assert second["already_running"] is True


def test_running_batch_is_never_returned_again(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=2)
    batch = ClassificationBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        status=ClassificationBatchStatus.RUNNING,
        article_count=2,
    )
    db_session.add(batch)
    db_session.add_all(
        ClassificationBatchArticle(batch_id=batch.id, article_id=a.id) for a in articles
    )
    db_session.commit()

    response = client.get(_batches_url(project.id, batch_size=40), headers=internal_headers)
    body = response.json()

    assert body["batches"] == []
    assert body["already_running"] is True
    assert db_session.query(ClassificationBatch).filter_by(project_id=project.id).count() == 1


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


def test_concurrent_batch_requests_do_not_create_duplicate_batches(
    client, internal_headers, db_session, project_factory, article_factory
):
    """Two near-simultaneous 'get next batch' calls for a project with no
    active batch yet must not both create one -- the project-row lock
    inside claim_next_classification_batch (and the partial unique index
    as a backstop) must serialize them.
    """
    project = project_factory()
    article_factory(project, count=40)
    # Captured before spawning threads: the shared db_session fixture (and
    # any ORM object bound to it, like `project`) is not thread-safe.
    project_id = project.id
    batches_url = _batches_url(project_id, batch_size=40)

    results: list[dict] = [None, None]  # type: ignore[list-item]

    def _call(index: int) -> None:
        response = client.get(batches_url, headers=internal_headers)
        results[index] = response.json()

    barrier = threading.Barrier(2)

    def _synchronized_call(index: int) -> None:
        barrier.wait()
        _call(index)

    threads = [threading.Thread(target=_synchronized_call, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with SessionLocal() as session:
        batches = (
            session.query(ClassificationBatch).filter_by(project_id=project_id).all()
        )
        assert len(batches) == 1

        links = (
            session.query(ClassificationBatchArticle)
            .join(ClassificationBatch)
            .filter(ClassificationBatch.project_id == project_id)
            .all()
        )
        article_ids = [link.article_id for link in links]
        assert len(article_ids) == len(set(article_ids)) == 40

    # Exactly one of the two responses got the real batch; the other saw it
    # already running.
    already_running_flags = [r["already_running"] for r in results]
    non_empty_batches = [r for r in results if r["batches"]]
    assert already_running_flags.count(True) == 1
    assert len(non_empty_batches) == 1
