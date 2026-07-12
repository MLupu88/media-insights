import uuid

from app.models.classification import (
    Classification,
    ClassificationBatch,
    ClassificationBatchArticle,
    ClassificationBatchStatus,
)
from app.models.project import AnalysisStatus


def _make_batch(db_session, project, articles, status=ClassificationBatchStatus.RUNNING):
    batch = ClassificationBatch(
        id=uuid.uuid4(), project_id=project.id, status=status, article_count=len(articles)
    )
    db_session.add(batch)
    db_session.add_all(
        ClassificationBatchArticle(batch_id=batch.id, article_id=article.id)
        for article in articles
    )
    db_session.commit()
    db_session.refresh(batch)
    return batch


def _add_classification(db_session, project, article):
    db_session.add(
        Classification(
            id=uuid.uuid4(),
            article_id=article.id,
            project_id=project.id,
            primary_topic="other",
            communication_category="incidental",
            sentiment="neutral",
            brand_role="incidental_mention",
            confidence=0.8,
            model="deepseek-chat",
            prompt_version="retail-deepseek-v2",
        )
    )
    db_session.commit()


def _complete_url(batch_id) -> str:
    return f"/api/internal/classification-batches/{batch_id}/complete"


def test_complete_batch_requires_internal_secret(client, project_factory, article_factory, db_session):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, articles)

    response = client.post(_complete_url(batch.id))
    assert response.status_code == 401


def test_complete_batch_succeeds_when_all_classifications_present(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=2)
    batch = _make_batch(db_session, project, articles)
    for article in articles:
        _add_classification(db_session, project, article)
    project.classified_rows = 2
    db_session.commit()

    response = client.post(_complete_url(batch.id), headers=internal_headers)

    assert response.status_code == 200
    assert response.json() == {"status": "complete", "batch_id": str(batch.id)}

    db_session.refresh(batch)
    assert batch.status == ClassificationBatchStatus.COMPLETE
    assert batch.completed_at is not None


def test_complete_batch_rejects_when_classifications_missing(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=2)
    batch = _make_batch(db_session, project, articles)
    _add_classification(db_session, project, articles[0])  # only one of two

    response = client.post(_complete_url(batch.id), headers=internal_headers)

    assert response.status_code == 422
    assert "missing" in response.json()["detail"].lower()

    db_session.refresh(batch)
    assert batch.status == ClassificationBatchStatus.RUNNING


def test_complete_batch_not_found(client, internal_headers):
    response = client.post(_complete_url(uuid.uuid4()), headers=internal_headers)
    assert response.status_code == 404


def test_complete_batch_updates_project_status_to_complete_when_fully_classified(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=2)
    project.valid_rows = 2
    project.classified_rows = 2
    db_session.commit()
    batch = _make_batch(db_session, project, articles)
    for article in articles:
        _add_classification(db_session, project, article)

    client.post(_complete_url(batch.id), headers=internal_headers)

    db_session.refresh(project)
    assert project.analysis_status == AnalysisStatus.COMPLETE


def test_complete_batch_updates_project_status_to_partially_complete(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    batch_articles = article_factory(project, count=2)
    remaining_articles = article_factory(project, count=2)
    project.valid_rows = 4
    project.classified_rows = 2
    db_session.commit()
    batch = _make_batch(db_session, project, batch_articles)
    for article in batch_articles:
        _add_classification(db_session, project, article)

    client.post(_complete_url(batch.id), headers=internal_headers)

    db_session.refresh(project)
    assert project.analysis_status == AnalysisStatus.PARTIALLY_COMPLETE
