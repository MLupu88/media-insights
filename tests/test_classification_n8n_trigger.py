from unittest.mock import patch

import httpx

from app.models.classification import ClassificationBatch, ClassificationBatchStatus
from app.models.project import AnalysisStatus


def _start_url(project_id) -> str:
    return f"/projects/{project_id}/classification/start"


def _mock_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=httpx.Request("POST", "https://example.test"))


def test_start_classification_requires_browser_session(client, project_factory):
    project = project_factory()

    response = client.post(_start_url(project.id), follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_start_classification_project_not_found(authenticated_client):
    import uuid

    response = authenticated_client.post(_start_url(uuid.uuid4()))
    assert response.status_code == 404


def test_start_classification_requires_valid_articles(
    authenticated_client, db_session, project_factory
):
    project = project_factory()

    response = authenticated_client.post(_start_url(project.id))

    assert response.status_code == 422
    assert "Import valid articles" in response.text

    db_session.refresh(project)
    assert project.analysis_status == AnalysisStatus.NOT_STARTED


@patch("app.services.n8n.httpx.post")
def test_start_classification_success_calls_n8n_and_updates_status(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1)

    response = authenticated_client.post(_start_url(project.id))

    assert response.status_code == 200
    assert "Classification started" in response.text

    db_session.refresh(project)
    assert project.analysis_status == AnalysisStatus.RUNNING

    assert mock_post.called
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["project_id"] == str(project.id)
    assert "secret" in kwargs["json"]
    assert "timeout" in kwargs


@patch("app.services.n8n.httpx.post")
def test_start_classification_does_not_leak_secret_in_html(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1)

    response = authenticated_client.post(_start_url(project.id))

    assert "test-internal-secret" not in response.text


@patch("app.services.n8n.httpx.post")
def test_start_classification_timeout_marks_project_failed(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.side_effect = httpx.TimeoutException("timed out")
    project = project_factory()
    article_factory(project, count=1)

    response = authenticated_client.post(_start_url(project.id))

    assert response.status_code == 502
    assert "timed out" in response.text.lower()

    db_session.refresh(project)
    assert project.analysis_status == AnalysisStatus.FAILED


@patch("app.services.n8n.httpx.post")
def test_start_classification_non_2xx_marks_project_failed(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(500)
    project = project_factory()
    article_factory(project, count=1)

    response = authenticated_client.post(_start_url(project.id))

    assert response.status_code == 502
    assert "unexpected status" in response.text.lower()

    db_session.refresh(project)
    assert project.analysis_status == AnalysisStatus.FAILED


@patch("app.services.n8n.httpx.post")
def test_start_classification_rejects_second_click_during_the_queued_window(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    """Covers the brief synchronous window between "queued" and the n8n
    trigger call resolving, before any ClassificationBatch row exists yet
    (so the active-batch check alone would not catch a second click here).
    A real double-click within this narrow window is only observable by
    setting the status directly, since a single-threaded test client always
    lets the first request run to completion before the second starts.
    """
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1)
    project.analysis_status = AnalysisStatus.QUEUED
    db_session.commit()

    response = authenticated_client.post(_start_url(project.id))

    assert response.status_code == 409
    assert "already in progress" in response.text.lower()
    assert mock_post.called is False


@patch("app.services.n8n.httpx.post")
def test_start_classification_resumable_when_status_is_stale_running_with_no_active_batch(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    """Guards against the exact failure mode the batching hotfix targets:
    if the async continuation after a completed batch never runs (process
    restart -- BackgroundTasks is not a durable queue) analysis_status can
    be left at "running" even though no batch is actually in flight. The
    button/route must still allow a fresh Start Classification click to
    resume, driven by real active-batch state, not the possibly-stale
    status column.
    """
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1)
    project.analysis_status = AnalysisStatus.RUNNING
    db_session.commit()

    response = authenticated_client.post(_start_url(project.id))

    assert response.status_code == 200
    assert mock_post.call_count == 1


@patch("app.services.n8n.httpx.post")
def test_start_classification_blocked_while_a_batch_is_actually_active(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=1)
    batch = ClassificationBatch(
        project_id=project.id,
        status=ClassificationBatchStatus.RUNNING,
        article_count=1,
    )
    db_session.add(batch)
    db_session.commit()
    project.analysis_status = AnalysisStatus.RUNNING
    db_session.commit()

    response = authenticated_client.post(_start_url(project.id))

    assert response.status_code == 409
    assert mock_post.called is False
