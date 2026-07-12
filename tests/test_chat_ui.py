from unittest.mock import patch

import httpx

from app.services.analytics import AnalyticsFilters
from app.services.chat_service import (
    create_run,
    find_or_create_comparison_session,
    find_or_create_project_session,
    process_answer,
    process_plan,
)
from app.schemas.chat import AnswerSubmission, PlanSubmission


def _mock_response(status_code: int) -> httpx.Response:
    return httpx.Response(
        status_code=status_code, request=httpx.Request("POST", "https://example.test")
    )


def _complete_a_question(db_session, session, question="Care este SOV pentru Auchan?"):
    run = create_run(db_session, session, question)
    plan = PlanSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        tool_calls=[{"tool": "get_brand_performance", "parameters": {"brand": "Auchan"}}],
    )
    outcome = process_plan(db_session, run, plan)
    sov = outcome.tool_results[0]["requested_brand"]["sov_pct"]
    answer = AnswerSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        answer_text=f"Auchan a avut un SOV de {sov}%.", answer_type="fact",
        evidence=[{"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": sov}],
        related_brand="Auchan",
    )
    process_answer(db_session, run, answer)
    return run


def test_chat_tab_empty_state_when_no_valid_articles(authenticated_client, project_factory):
    project = project_factory()
    response = authenticated_client.get(f"/projects/{project.id}?tab=chat")
    assert response.status_code == 200
    assert "No valid articles to ask about yet" in response.text


def test_chat_tab_shows_no_questions_yet_when_no_session(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=chat")
    assert response.status_code == 200
    assert "No questions asked yet." in response.text


def test_chat_tab_renders_completed_conversation(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    session = find_or_create_project_session(db_session, project, AnalyticsFilters())
    _complete_a_question(db_session, session)

    response = authenticated_client.get(f"/projects/{project.id}?tab=chat")
    assert "Care este SOV pentru Auchan?" in response.text
    assert "Auchan a avut un SOV de" in response.text
    assert "fact" in response.text.lower()


def test_chat_tab_renders_evidence_and_tool_calls_collapsed(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()
    session = find_or_create_project_session(db_session, project, AnalyticsFilters())
    _complete_a_question(db_session, session)

    response = authenticated_client.get(f"/projects/{project.id}?tab=chat")
    assert "requested_brand.sov_pct" in response.text
    assert "<details" in response.text
    assert "Tool calls" in response.text


@patch("app.services.n8n.httpx.post")
def test_chat_tab_shows_retry_affordance_on_failed_run(
    mock_post, authenticated_client, db_session, project_factory, article_factory
):
    mock_post.side_effect = httpx.TimeoutException("timed out")
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    authenticated_client.post(f"/projects/{project.id}/chat/ask", data={"question": "Q?"})
    response = authenticated_client.get(f"/projects/{project.id}?tab=chat")

    assert "could not be answered" in response.text.lower()
    assert "/retry" in response.text


def test_chat_session_detail_renders_project_scope(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project, AnalyticsFilters())
    _complete_a_question(db_session, session)

    response = authenticated_client.get(f"/chat-sessions/{session.id}")
    assert response.status_code == 200
    assert "Project chat" in response.text
    assert "Auchan a avut un SOV de" in response.text


def test_chat_session_detail_renders_comparison_scope(
    authenticated_client, db_session, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    session = find_or_create_comparison_session(db_session, [a.id], [b.id])
    run = create_run(db_session, session, "Cum s-a schimbat SOV pentru Auchan?")
    plan = PlanSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        tool_calls=[{"tool": "get_period_comparison", "parameters": {}}],
    )
    outcome = process_plan(db_session, run, plan)
    delta = outcome.tool_results[0]["deltas"]["kpis"]["unique_valid_articles"]["absolute_delta"]
    answer = AnswerSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        answer_text=f"Numarul de articole s-a schimbat cu {delta}.", answer_type="fact",
        evidence=[{"kind": "metric", "tool_call_index": 0, "path": "deltas.kpis.unique_valid_articles.absolute_delta", "role": "delta", "value": delta}],
    )
    process_answer(db_session, run, answer)

    response = authenticated_client.get(f"/chat-sessions/{session.id}")
    assert response.status_code == 200
    assert "Comparison chat" in response.text
    assert "Numarul de articole s-a schimbat" in response.text


def test_compare_page_shows_ask_form_when_no_session(
    authenticated_client, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={a.id}&comparison_project_ids={b.id}"
    )
    assert response.status_code == 200
    assert "Ask about this comparison" in response.text
    assert 'action="/compare/chat/ask"' in response.text


def test_compare_page_shows_view_conversation_link_when_session_exists(
    authenticated_client, db_session, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")
    session = find_or_create_comparison_session(db_session, [a.id], [b.id])

    response = authenticated_client.get(
        f"/compare?baseline_project_ids={a.id}&comparison_project_ids={b.id}"
    )
    assert response.status_code == 200
    assert f"/chat-sessions/{session.id}" in response.text
    assert "View conversation" in response.text


def test_partially_classified_project_can_still_be_asked_about(
    db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    articles = article_factory(project, count=3, retailer="Auchan")
    classification_factory(articles[0])

    session = find_or_create_project_session(db_session, project, AnalyticsFilters())
    run = create_run(db_session, session, "Cate articole sunt clasificate?")
    plan = PlanSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        tool_calls=[{"tool": "get_project_kpis", "parameters": {}}],
    )
    outcome = process_plan(db_session, run, plan)
    assert outcome.tool_results[0]["kpis"]["unique_classified_articles"] == 1
    assert outcome.tool_results[0]["kpis"]["unique_unclassified_articles"] == 2


def test_no_secret_leakage_in_chat_session_detail(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project, AnalyticsFilters())
    _complete_a_question(db_session, session)

    response = authenticated_client.get(f"/chat-sessions/{session.id}")
    assert "test-internal-secret" not in response.text
