import threading
import uuid
from unittest.mock import patch

import httpx

from app.database import SessionLocal

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


def _mock_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=httpx.Request("POST", "https://example.test"))


def test_complete_batch_requires_internal_secret(client, project_factory, article_factory, db_session):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, articles)

    response = client.post(_complete_url(batch.id))
    assert response.status_code == 401


@patch("app.services.n8n.httpx.post")
def test_complete_batch_succeeds_when_all_classifications_present(
    mock_post, client, internal_headers, db_session, project_factory, article_factory
):
    # No unclassified articles remain, so no continuation is triggered.
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    articles = article_factory(project, count=2)
    batch = _make_batch(db_session, project, articles)
    for article in articles:
        _add_classification(db_session, project, article)
    project.classified_rows = 2
    project.valid_rows = 2
    db_session.commit()

    response = client.post(_complete_url(batch.id), headers=internal_headers)

    assert response.status_code == 202
    assert response.json() == {"status": "complete", "batch_id": str(batch.id)}

    db_session.refresh(batch)
    assert batch.status == ClassificationBatchStatus.COMPLETE
    assert batch.completed_at is not None
    assert mock_post.called is False


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

    # Per the robustness hotfix: a batch that fails to complete is marked
    # failed (not left running) so it is never stuck, and its articles
    # become eligible for a fresh batch.
    db_session.refresh(batch)
    assert batch.status == ClassificationBatchStatus.FAILED
    assert batch.error_message


def test_complete_batch_not_found(client, internal_headers):
    response = client.post(_complete_url(uuid.uuid4()), headers=internal_headers)
    assert response.status_code == 404


@patch("app.services.n8n.httpx.post")
def test_complete_batch_updates_project_status_to_complete_when_fully_classified(
    mock_post, client, internal_headers, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
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
    assert mock_post.called is False


@patch("app.services.n8n.httpx.post")
def test_complete_batch_stays_running_and_triggers_continuation_when_articles_remain(
    mock_post, client, internal_headers, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    batch_articles = article_factory(project, count=2)
    article_factory(project, count=2)
    project.valid_rows = 4
    project.classified_rows = 2
    db_session.commit()
    batch = _make_batch(db_session, project, batch_articles)
    for article in batch_articles:
        _add_classification(db_session, project, article)

    response = client.post(_complete_url(batch.id), headers=internal_headers)

    assert response.status_code == 202
    db_session.refresh(project)
    # Remaining eligible articles exist and a continuation was scheduled,
    # so the project reads as still running (see requirement #4: "batch
    # complet si mai exista articole: ramane running").
    assert project.analysis_status == AnalysisStatus.RUNNING
    assert mock_post.called is True


# --- idempotency (requirement 3) --------------------------------------------


@patch("app.services.n8n.httpx.post")
def test_complete_batch_called_twice_does_not_trigger_two_continuations(
    mock_post, client, internal_headers, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    batch_articles = article_factory(project, count=1)
    article_factory(project, count=1)  # remaining, unbatched, eligible article
    project.valid_rows = 2
    project.classified_rows = 1
    db_session.commit()
    batch = _make_batch(db_session, project, batch_articles)
    _add_classification(db_session, project, batch_articles[0])

    first = client.post(_complete_url(batch.id), headers=internal_headers)
    second = client.post(_complete_url(batch.id), headers=internal_headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == {"status": "complete", "batch_id": str(batch.id)}
    assert mock_post.call_count == 1

    db_session.refresh(batch)
    assert batch.status == ClassificationBatchStatus.COMPLETE


# --- async continuation (requirements 4, 5, 10-e) ---------------------------


@patch("app.services.n8n.httpx.post")
def test_complete_batch_triggers_continuation_when_articles_remain(
    mock_post, client, internal_headers, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    batch_articles = article_factory(project, count=1)
    article_factory(project, count=1)  # remaining, unbatched, eligible article
    project.valid_rows = 2
    project.classified_rows = 1
    db_session.commit()
    batch = _make_batch(db_session, project, batch_articles)
    _add_classification(db_session, project, batch_articles[0])

    response = client.post(_complete_url(batch.id), headers=internal_headers)

    assert response.status_code == 202
    assert mock_post.call_count == 1
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["project_id"] == str(project.id)


# --- recoverability when the async dispatch fails (requirement 2) ----------


@patch("app.services.n8n.httpx.post")
def test_failed_continuation_dispatch_leaves_project_resumable(
    mock_post, client, internal_headers, db_session, project_factory, article_factory
):
    mock_post.side_effect = httpx.TimeoutException("timed out")
    project = project_factory()
    batch_articles = article_factory(project, count=1)
    article_factory(project, count=1)  # remaining, unbatched, eligible article
    project.valid_rows = 2
    project.classified_rows = 1
    db_session.commit()
    batch = _make_batch(db_session, project, batch_articles)
    _add_classification(db_session, project, batch_articles[0])

    response = client.post(_complete_url(batch.id), headers=internal_headers)

    assert response.status_code == 202  # the response itself never waits on the dispatch

    db_session.refresh(project)
    # No new batch was left pending/running as a result of the failed
    # dispatch, and the project's status is no longer "running"/"queued",
    # so Start Classification is clickable again.
    assert (
        db_session.query(ClassificationBatch)
        .filter_by(project_id=project.id, status="running")
        .count()
        == 0
    )
    assert project.analysis_status not in (AnalysisStatus.QUEUED, AnalysisStatus.RUNNING)


# --- explicit terminal-state handling ---------------------------------------


def test_complete_batch_on_an_already_failed_batch_is_rejected_not_completed(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = _make_batch(db_session, project, articles, status=ClassificationBatchStatus.FAILED)
    batch.error_message = "Classification batch failed."
    db_session.commit()

    response = client.post(_complete_url(batch.id), headers=internal_headers)

    assert response.status_code == 409

    db_session.refresh(batch)
    assert batch.status == ClassificationBatchStatus.FAILED
    assert batch.error_message == "Classification batch failed."  # untouched, not re-marked


# --- concurrent completion (requirement 1 of the follow-up correction) -----


@patch("app.services.n8n.httpx.post")
def test_concurrent_complete_requests_do_not_double_complete_or_double_trigger(
    mock_post, client, internal_headers, db_session, project_factory, article_factory
):
    """Two near-simultaneous "Complete Batch" calls for the same batch (a
    realistic retry from n8n, or a duplicate callback) must not both
    observe it as running and both flip it to complete: only the request
    that actually performs the transition may schedule the continuation.
    """
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    batch_articles = article_factory(project, count=1)
    article_factory(project, count=1)  # remaining, unbatched, eligible article
    project.valid_rows = 2
    project.classified_rows = 1
    db_session.commit()
    batch = _make_batch(db_session, project, batch_articles)
    _add_classification(db_session, project, batch_articles[0])
    # Captured before spawning threads: the shared db_session fixture (and
    # any ORM object bound to it, like `batch`) is not thread-safe, so the
    # thread bodies below must only ever touch these plain values, never
    # lazily re-touch `batch`/`project` attributes concurrently.
    batch_id = batch.id
    complete_url = _complete_url(batch_id)

    results: list[httpx.Response] = [None, None]  # type: ignore[list-item]

    def _call(index: int) -> None:
        results[index] = client.post(complete_url, headers=internal_headers)

    barrier = threading.Barrier(2)

    def _synchronized_call(index: int) -> None:
        barrier.wait()
        _call(index)

    threads = [threading.Thread(target=_synchronized_call, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for response in results:
        assert response.status_code == 202
        assert response.json() == {"status": "complete", "batch_id": str(batch_id)}

    with SessionLocal() as session:
        refreshed = session.get(ClassificationBatch, batch_id)
        assert refreshed.status == ClassificationBatchStatus.COMPLETE

        batch_count = (
            session.query(ClassificationBatch).filter_by(project_id=project.id).count()
        )
        assert batch_count == 1  # no second/next batch was created as a side effect

    assert mock_post.call_count == 1
