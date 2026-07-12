from unittest.mock import patch

import httpx


def _mock_response(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code=status_code, request=httpx.Request("POST", "https://example.test")
    )


def _bootstrap_project_run(db_session, project_factory, article_factory, count=1):
    from app.services.analytics import AnalyticsFilters
    from app.services.chat_service import create_run, find_or_create_project_session

    project = project_factory()
    article_factory(project, count=count, retailer="Auchan")
    session = find_or_create_project_session(db_session, project, AnalyticsFilters())
    run = create_run(db_session, session, "Care este SOV pentru Auchan?")
    return project, session, run


def _run_plan(client, headers, run_id, payload_schema_version):
    body = {
        "model": "m", "prompt_version": "v1", "payload_schema_version": payload_schema_version,
        "tool_calls": [{"tool": "get_brand_performance", "parameters": {"brand": "Auchan"}}],
    }
    return client.post(f"/api/internal/chat-runs/{run_id}/plan", json=body, headers=headers)


# --- Internal API --------------------------------------------------------------


def test_planning_payload_requires_internal_secret(client, db_session, project_factory, article_factory):
    _, _, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    response = client.get(f"/api/internal/chat-runs/{run.id}/planning-payload")
    assert response.status_code == 401


def test_planning_payload_returns_persisted_snapshot(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, _, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    response = client.get(f"/api/internal/chat-runs/{run.id}/planning-payload", headers=internal_headers)
    assert response.status_code == 200
    assert response.json()["snapshot"]["question"] == "Care este SOV pentru Auchan?"


def test_plan_endpoint_executes_and_returns_tool_results(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, _, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    response = _run_plan(client, internal_headers, run.id, run.payload_schema_version)
    assert response.status_code == 200
    assert response.json()["tool_results"][0]["requested_brand"]["brand"] == "Auchan"


def test_plan_endpoint_rejects_unknown_tool(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, _, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    body = {
        "model": "m", "prompt_version": "v1", "payload_schema_version": run.payload_schema_version,
        "tool_calls": [{"tool": "get_secret_data", "parameters": {}}],
    }
    response = client.post(f"/api/internal/chat-runs/{run.id}/plan", json=body, headers=internal_headers)
    assert response.status_code == 422


def test_plan_endpoint_rejects_too_many_tool_calls(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, _, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    body = {
        "model": "m", "prompt_version": "v1", "payload_schema_version": run.payload_schema_version,
        "tool_calls": [{"tool": "get_project_kpis", "parameters": {}} for _ in range(5)],
    }
    response = client.post(f"/api/internal/chat-runs/{run.id}/plan", json=body, headers=internal_headers)
    assert response.status_code == 422


def test_plan_identical_retry_replays_without_reexecuting(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, _, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    body = {
        "model": "m", "prompt_version": "v1", "payload_schema_version": run.payload_schema_version,
        "tool_calls": [{"tool": "get_brand_performance", "parameters": {"brand": "Auchan"}}],
    }
    first = client.post(f"/api/internal/chat-runs/{run.id}/plan", json=body, headers=internal_headers)
    second = client.post(f"/api/internal/chat-runs/{run.id}/plan", json=body, headers=internal_headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


def test_plan_conflicting_resubmission_rejected(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, _, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    _run_plan(client, internal_headers, run.id, run.payload_schema_version)
    conflicting_body = {
        "model": "m", "prompt_version": "v1", "payload_schema_version": run.payload_schema_version,
        "tool_calls": [{"tool": "get_project_kpis", "parameters": {}}],
    }
    response = client.post(
        f"/api/internal/chat-runs/{run.id}/plan", json=conflicting_body, headers=internal_headers
    )
    assert response.status_code == 409


def test_answer_endpoint_validates_and_completes(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, session, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    plan_response = _run_plan(client, internal_headers, run.id, run.payload_schema_version)
    sov = plan_response.json()["tool_results"][0]["requested_brand"]["sov_pct"]

    answer_body = {
        "model": "m", "prompt_version": "v1", "payload_schema_version": run.payload_schema_version,
        "answer_text": f"Auchan a avut un SOV de {sov}%.", "answer_type": "fact",
        "evidence": [{"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": sov}],
        "related_brand": "Auchan",
    }
    response = client.post(f"/api/internal/chat-runs/{run.id}/answer", json=answer_body, headers=internal_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "complete"

    session_response = client.get(f"/api/internal/chat-sessions/{session.id}", headers=internal_headers)
    roles = [m["role"] for m in session_response.json()["messages"]]
    assert roles == ["user", "assistant"]


def test_answer_endpoint_rejects_unsupported_claim(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, session, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    _run_plan(client, internal_headers, run.id, run.payload_schema_version)

    answer_body = {
        "model": "m", "prompt_version": "v1", "payload_schema_version": run.payload_schema_version,
        "answer_text": "Auchan a avut 999999 de articole.", "answer_type": "fact",
        "evidence": [{"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": 0.1}],
    }
    response = client.post(f"/api/internal/chat-runs/{run.id}/answer", json=answer_body, headers=internal_headers)
    assert response.status_code == 422

    session_response = client.get(f"/api/internal/chat-sessions/{session.id}", headers=internal_headers)
    assert len(session_response.json()["messages"]) == 1  # rejected answer never becomes a message


def test_answer_identical_retry_after_rejection_replays_422(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, _, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    _run_plan(client, internal_headers, run.id, run.payload_schema_version)
    bad_body = {
        "model": "m", "prompt_version": "v1", "payload_schema_version": run.payload_schema_version,
        "answer_text": "Auchan a avut 999999 de articole.", "answer_type": "fact",
        "evidence": [{"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": 0.1}],
    }
    first = client.post(f"/api/internal/chat-runs/{run.id}/answer", json=bad_body, headers=internal_headers)
    second = client.post(f"/api/internal/chat-runs/{run.id}/answer", json=bad_body, headers=internal_headers)
    assert first.status_code == 422
    assert second.status_code == 422
    assert second.json()["detail"] == first.json()["detail"]


def test_audit_endpoint_exposes_rejected_content(
    client, internal_headers, db_session, project_factory, article_factory
):
    _, _, run = _bootstrap_project_run(db_session, project_factory, article_factory)
    _run_plan(client, internal_headers, run.id, run.payload_schema_version)
    bad_body = {
        "model": "m", "prompt_version": "v1", "payload_schema_version": run.payload_schema_version,
        "answer_text": "Auchan a avut 999999 de articole.", "answer_type": "fact",
        "evidence": [{"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": 0.1}],
    }
    client.post(f"/api/internal/chat-runs/{run.id}/answer", json=bad_body, headers=internal_headers)

    audit = client.get(f"/api/internal/chat-runs/{run.id}", headers=internal_headers)
    assert audit.status_code == 200
    assert audit.json()["validation_status"] == "rejected"
    assert audit.json()["answer_text"] == "Auchan a avut 999999 de articole."


def test_list_project_chat_sessions(
    client, internal_headers, db_session, project_factory, article_factory
):
    project, _, _ = _bootstrap_project_run(db_session, project_factory, article_factory)
    response = client.get(f"/api/internal/projects/{project.id}/chat-sessions", headers=internal_headers)
    assert response.status_code == 200
    assert response.json()[0]["scope"] == "project"


# --- Browser routes --------------------------------------------------------------


def test_ask_project_chat_requires_session(client, project_factory):
    project = project_factory()
    response = client.post(f"/projects/{project.id}/chat/ask", data={"question": "Q?"}, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_ask_project_chat_not_found(authenticated_client):
    import uuid
    response = authenticated_client.post(f"/projects/{uuid.uuid4()}/chat/ask", data={"question": "Q?"})
    assert response.status_code == 404


@patch("app.services.n8n.httpx.post")
def test_ask_project_chat_success(mock_post, authenticated_client, db_session, project_factory, article_factory):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    response = authenticated_client.post(f"/projects/{project.id}/chat/ask", data={"question": "Care este SOV?"})
    assert response.status_code == 200
    assert "test-internal-secret" not in response.text
    assert mock_post.called
    _, kwargs = mock_post.call_args
    assert "secret" in kwargs["json"]


@patch("app.services.n8n.httpx.post")
def test_ask_project_chat_timeout_marks_run_failed(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.side_effect = httpx.TimeoutException("timed out")
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    response = authenticated_client.post(f"/projects/{project.id}/chat/ask", data={"question": "Q?"})
    assert response.status_code == 200
    assert "could not be answered" in response.text.lower() or "failed" in response.text.lower()


@patch("app.services.n8n.httpx.post")
def test_repeated_browser_submission_does_not_duplicate_message(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    authenticated_client.post(f"/projects/{project.id}/chat/ask", data={"question": "Q?"})
    second = authenticated_client.post(f"/projects/{project.id}/chat/ask", data={"question": "Second?"})

    assert second.status_code == 409
    from app.models.chat import ChatMessage, ChatSession

    session = db_session.query(ChatSession).filter_by(project_id=project.id).one()
    messages = db_session.query(ChatMessage).filter_by(session_id=session.id).all()
    assert len(messages) == 1


@patch("app.services.n8n.httpx.post")
def test_retry_only_from_failed_run(mock_post, authenticated_client, db_session, project_factory, article_factory):
    mock_post.return_value = _mock_response(200)
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()
    authenticated_client.post(f"/projects/{project.id}/chat/ask", data={"question": "Q?"})

    from app.models.chat import ChatRun

    run = db_session.query(ChatRun).filter_by(session_id=db_session.query(ChatRun).first().session_id).first()
    response = authenticated_client.post(f"/chat-runs/{run.id}/retry")
    assert response.status_code == 409  # run is "pending", not "failed"


@patch("app.services.n8n.httpx.post")
def test_retry_after_n8n_failure_succeeds(mock_post, authenticated_client, db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    mock_post.side_effect = httpx.TimeoutException("timed out")
    authenticated_client.post(f"/projects/{project.id}/chat/ask", data={"question": "Q?"})

    from app.models.chat import ChatRun

    failed_run = db_session.query(ChatRun).filter_by(status="failed").one()

    mock_post.side_effect = None
    mock_post.return_value = _mock_response(200)
    response = authenticated_client.post(f"/chat-runs/{failed_run.id}/retry")
    assert response.status_code == 200

    db_session.refresh(failed_run)
    new_run = db_session.query(ChatRun).filter_by(retry_of_run_id=failed_run.id).one()
    assert new_run.status == "pending"


@patch("app.services.n8n.httpx.post")
def test_ask_comparison_chat_redirects_to_session_detail(
    mock_post, authenticated_client, project_factory, article_factory
):
    mock_post.return_value = _mock_response(200)
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    response = authenticated_client.post(
        "/compare/chat/ask",
        data={"baseline_project_ids": [str(a.id)], "comparison_project_ids": [str(b.id)], "question": "Q?"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/chat-sessions/")

    detail = authenticated_client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "Comparison chat" in detail.text


def test_chat_session_detail_not_found(authenticated_client):
    import uuid
    response = authenticated_client.get(f"/chat-sessions/{uuid.uuid4()}")
    assert response.status_code == 404


def test_ask_project_and_internal_api_report_matching_messages(
    client, internal_headers, authenticated_client, db_session, project_factory, article_factory
):
    """project/API/UI consistency: the browser tab and the internal session
    endpoint must read the exact same message history for one session.
    """
    with patch("app.services.n8n.httpx.post") as mock_post:
        mock_post.return_value = _mock_response(200)
        project = project_factory()
        article_factory(project, count=1, retailer="Auchan")
        project.valid_rows = 1
        db_session.commit()

        authenticated_client.post(f"/projects/{project.id}/chat/ask", data={"question": "Care este SOV?"})

    ui_response = authenticated_client.get(f"/projects/{project.id}?tab=chat")
    api_response = client.get(f"/api/internal/projects/{project.id}/chat-sessions", headers=internal_headers)
    session_id = api_response.json()[0]["id"]
    session_detail = client.get(f"/api/internal/chat-sessions/{session_id}", headers=internal_headers)

    assert session_detail.json()["messages"][0]["content"] == "Care este SOV?"
    assert "Care este SOV?" in ui_response.text
