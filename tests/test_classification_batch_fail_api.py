import uuid

from app.models.classification import (
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


def _url(batch_id):
    return f"/api/internal/classification-batches/{batch_id}/fail"


def test_fail_batch_requires_internal_secret(client, db_session, project_factory, article_factory):
    project = project_factory()
    batch = _make_batch(db_session, project, article_factory(project, count=1))

    response = client.post(_url(batch.id), json={"error_message": "invalid model output"})

    assert response.status_code == 401


def test_fail_running_batch_is_terminal_and_resumable(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    project.valid_rows = 2
    project.classified_rows = 1
    db_session.commit()
    batch = _make_batch(db_session, project, article_factory(project, count=1))

    response = client.post(
        _url(batch.id),
        headers=internal_headers,
        json={"error_message": "DeepSeek retry returned invalid JSON"},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "failed", "batch_id": str(batch.id)}
    db_session.refresh(batch)
    db_session.refresh(project)
    assert batch.status == ClassificationBatchStatus.FAILED
    assert batch.completed_at is not None
    assert batch.error_message == "DeepSeek retry returned invalid JSON"
    assert project.analysis_status == AnalysisStatus.PARTIALLY_COMPLETE


def test_fail_batch_is_idempotent(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    batch = _make_batch(
        db_session, project, article_factory(project, count=1), ClassificationBatchStatus.FAILED
    )
    batch.error_message = "original error"
    db_session.commit()

    response = client.post(
        _url(batch.id), headers=internal_headers, json={"error_message": "replacement error"}
    )

    assert response.status_code == 202
    db_session.refresh(batch)
    assert batch.status == ClassificationBatchStatus.FAILED
    assert batch.error_message == "original error"


def test_completed_batch_cannot_be_marked_failed(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    batch = _make_batch(
        db_session, project, article_factory(project, count=1), ClassificationBatchStatus.COMPLETE
    )

    response = client.post(
        _url(batch.id), headers=internal_headers, json={"error_message": "late failure"}
    )

    assert response.status_code == 409
    db_session.refresh(batch)
    assert batch.status == ClassificationBatchStatus.COMPLETE


def test_fail_batch_not_found(client, internal_headers):
    response = client.post(
        _url(uuid.uuid4()), headers=internal_headers, json={"error_message": "missing"}
    )
    assert response.status_code == 404
