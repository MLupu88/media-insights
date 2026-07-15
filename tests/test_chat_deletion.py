import uuid

from sqlalchemy import select

from app.models.article import Article
from app.models.chat import ChatMessage, ChatRun, ChatSession
from app.models.project import Project
from app.schemas.chat import AnswerSubmission, PlanSubmission
from app.services.analytics import AnalyticsFilters
from app.services.chat_service import (
    create_run,
    find_or_create_comparison_session,
    find_or_create_project_session,
    process_answer,
    process_plan,
)


def _complete_a_question(db_session, session, question="Care este SOV pentru Auchan?"):
    """Returns (run_id, user_message_id) as plain UUIDs, not live ORM
    objects -- db_session.rollback() (used after every delete below to
    discard the request's own session's stale identity-map cache) expires
    every attribute on every tracked object, including primary keys. If a
    row has since been deleted by that other session, touching so much as
    `.id` on the now-stale local object re-triggers a reload and raises
    ObjectDeletedError instead of returning a value. Capturing IDs as plain
    UUIDs up front, before any delete/rollback, avoids the whole class of
    bug.
    """
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
    return run.id, run.user_message_id


def _exists(db_session, model, id_) -> bool:
    """A fresh SELECT, never Session.get() or a live ORM object's `.id` --
    see _complete_a_question's docstring for why touching an expired,
    since-deleted object raises ObjectDeletedError instead of returning
    None/False.
    """
    return db_session.execute(select(model).where(model.id == id_)).scalar_one_or_none() is not None


def _delete_url(project_id, message_id) -> str:
    return f"/projects/{project_id}/chat/messages/{message_id}/delete"


def _delete_all_url(project_id) -> str:
    return f"/projects/{project_id}/chat/delete-all"


def _setup_project_with_question(db_session, project_factory, article_factory, question="Care este SOV pentru Auchan?"):
    """Returns (project, session_id, run_id, user_message_id). `project` is
    safe to keep as a live object -- it is never deleted by anything under
    test here. The chat entities are returned as plain UUIDs (see
    _complete_a_question).
    """
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()
    session = find_or_create_project_session(db_session, project, AnalyticsFilters())
    session_id = session.id
    run_id, user_message_id = _complete_a_question(db_session, session, question)
    return project, session_id, run_id, user_message_id


# --- delete one conversation --------------------------------------------------


def test_delete_one_conversation_removes_it_and_redirects_to_chat_tab(
    authenticated_client, db_session, project_factory, article_factory
):
    project, session_id, run_id, user_message_id = _setup_project_with_question(
        db_session, project_factory, article_factory
    )

    response = authenticated_client.post(
        _delete_url(project.id, user_message_id), follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}?tab=chat&chat_deleted=1"

    follow_up = authenticated_client.get(response.headers["location"])
    assert follow_up.status_code == 200
    assert "The conversation was deleted." in follow_up.text
    assert "Care este SOV" not in follow_up.text


def test_dependent_chat_records_are_deleted(
    authenticated_client, db_session, project_factory, article_factory
):
    project, session_id, run_id, user_message_id = _setup_project_with_question(
        db_session, project_factory, article_factory
    )
    assistant_message_id = db_session.execute(
        select(ChatMessage.id).where(ChatMessage.run_id == run_id)
    ).scalar_one()

    authenticated_client.post(_delete_url(project.id, user_message_id))

    db_session.rollback()
    assert not _exists(db_session, ChatMessage, user_message_id)
    assert not _exists(db_session, ChatMessage, assistant_message_id)
    assert not _exists(db_session, ChatRun, run_id)
    # The session itself is untouched by a single-exchange delete.
    assert _exists(db_session, ChatSession, session_id)


def test_other_conversations_in_the_same_project_remain(
    authenticated_client, db_session, project_factory, article_factory
):
    project, session_id, first_run_id, first_message_id = _setup_project_with_question(
        db_session, project_factory, article_factory, question="Prima intrebare?"
    )
    session = db_session.get(ChatSession, session_id)
    second_run_id, second_message_id = _complete_a_question(
        db_session, session, question="A doua intrebare?"
    )

    authenticated_client.post(_delete_url(project.id, first_message_id))

    db_session.rollback()
    assert not _exists(db_session, ChatMessage, first_message_id)
    assert _exists(db_session, ChatMessage, second_message_id)
    assert _exists(db_session, ChatRun, second_run_id)

    follow_up = authenticated_client.get(f"/projects/{project.id}?tab=chat")
    assert "A doua intrebare?" in follow_up.text
    assert "Prima intrebare?" not in follow_up.text


def test_conversations_in_another_project_remain(
    authenticated_client, db_session, project_factory, article_factory
):
    project_a, session_a_id, run_a_id, message_a_id = _setup_project_with_question(
        db_session, project_factory, article_factory, question="Intrebare proiect A?"
    )
    project_b, session_b_id, run_b_id, message_b_id = _setup_project_with_question(
        db_session, project_factory, article_factory, question="Intrebare proiect B?"
    )

    authenticated_client.post(_delete_url(project_a.id, message_a_id))

    db_session.rollback()
    assert not _exists(db_session, ChatMessage, message_a_id)
    assert _exists(db_session, ChatMessage, message_b_id)
    assert _exists(db_session, ChatSession, session_b_id)

    follow_up = authenticated_client.get(f"/projects/{project_b.id}?tab=chat")
    assert "Intrebare proiect B?" in follow_up.text


def test_mismatched_project_and_conversation_cannot_delete_data(
    authenticated_client, db_session, project_factory, article_factory
):
    project_a, session_a_id, run_a_id, message_a_id = _setup_project_with_question(
        db_session, project_factory, article_factory
    )
    project_b = project_factory(name="Unrelated Project")

    response = authenticated_client.post(_delete_url(project_b.id, message_a_id))

    assert response.status_code == 404
    db_session.rollback()
    assert _exists(db_session, ChatMessage, message_a_id)
    assert _exists(db_session, ChatRun, run_a_id)


def test_comparison_session_is_never_deleted_via_project_chat_routes(
    authenticated_client, db_session, project_factory, article_factory
):
    """Requirement 10: preserve comparison-chat behaviour. A comparison
    session is anchored (project_id) at the first baseline project, but
    must never be reachable through that project's own chat-tab deletion
    routes.
    """
    project_a = project_factory(name="Comparison Baseline")
    project_b = project_factory(name="Comparison Target")
    article_factory(project_a, count=1, retailer="Auchan")
    db_session.commit()

    comparison_session = find_or_create_comparison_session(
        db_session, [project_a.id], [project_b.id], AnalyticsFilters()
    )
    comparison_session_id = comparison_session.id
    # A pending run (no plan/answer) is enough to prove the comparison
    # session and its message are untouched -- completing a full
    # plan/answer round trip isn't needed and isn't valid for every tool
    # under comparison scope.
    comparison_run = create_run(db_session, comparison_session, "Comparatie intrebare?")
    comparison_message_id = comparison_run.user_message_id

    # Attempting to delete the comparison exchange through project A's own
    # chat routes must fail safely (404), and delete-all for project A must
    # never touch it either.
    response = authenticated_client.post(_delete_url(project_a.id, comparison_message_id))
    assert response.status_code == 404

    authenticated_client.post(_delete_all_url(project_a.id))

    db_session.rollback()
    assert _exists(db_session, ChatSession, comparison_session_id)
    assert _exists(db_session, ChatMessage, comparison_message_id)


# --- delete all ----------------------------------------------------------------


def test_delete_all_removes_only_chat_data_for_the_selected_project(
    authenticated_client, db_session, project_factory, article_factory
):
    project_a, session_a_id, run_a_id, message_a_id = _setup_project_with_question(
        db_session, project_factory, article_factory, question="Proiect A intrebare?"
    )
    project_b, session_b_id, run_b_id, message_b_id = _setup_project_with_question(
        db_session, project_factory, article_factory, question="Proiect B intrebare?"
    )
    project_a_id = project_a.id

    response = authenticated_client.post(_delete_all_url(project_a_id), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project_a_id}?tab=chat&chat_deleted_all=1"

    db_session.rollback()
    assert not _exists(db_session, ChatSession, session_a_id)
    assert not _exists(db_session, ChatMessage, message_a_id)
    assert not _exists(db_session, ChatRun, run_a_id)

    assert _exists(db_session, ChatSession, session_b_id)
    assert _exists(db_session, ChatMessage, message_b_id)

    # Articles/project rows are never touched by chat deletion.
    assert _exists(db_session, Project, project_a_id)


def test_delete_all_does_not_touch_articles_or_project(
    authenticated_client, db_session, project_factory, article_factory
):
    project, session_id, run_id, message_id = _setup_project_with_question(
        db_session, project_factory, article_factory
    )
    project_id = project.id
    article_ids = [a.id for a in project.articles]

    authenticated_client.post(_delete_all_url(project_id))

    db_session.rollback()
    assert _exists(db_session, Project, project_id)
    for article_id in article_ids:
        assert _exists(db_session, Article, article_id)


def test_delete_all_empty_state(authenticated_client, db_session, project_factory):
    project = project_factory()

    response = authenticated_client.post(_delete_all_url(project.id), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}?tab=chat&chat_deleted_all=0"

    follow_up = authenticated_client.get(response.headers["location"])
    assert "There were no conversations to delete." in follow_up.text


def test_chat_tab_hides_delete_all_when_there_is_nothing_to_delete(
    authenticated_client, project_factory, article_factory, db_session
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=chat")

    assert response.status_code == 200
    assert "Delete all conversations" not in response.text


def test_chat_tab_shows_delete_all_when_a_conversation_exists(
    authenticated_client, db_session, project_factory, article_factory
):
    project, session_id, run_id, message_id = _setup_project_with_question(
        db_session, project_factory, article_factory
    )

    response = authenticated_client.get(f"/projects/{project.id}?tab=chat")

    assert response.status_code == 200
    assert "Delete all conversations" in response.text


# --- authentication --------------------------------------------------------------


def test_unauthenticated_single_delete_is_redirected_to_login(
    client, db_session, project_factory, article_factory
):
    project, session_id, run_id, message_id = _setup_project_with_question(
        db_session, project_factory, article_factory
    )

    response = client.post(_delete_url(project.id, message_id), follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"

    db_session.rollback()
    assert _exists(db_session, ChatMessage, message_id)


def test_unauthenticated_delete_all_is_redirected_to_login(
    client, db_session, project_factory, article_factory
):
    project, session_id, run_id, message_id = _setup_project_with_question(
        db_session, project_factory, article_factory
    )

    response = client.post(_delete_all_url(project.id), follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"

    db_session.rollback()
    assert _exists(db_session, ChatSession, session_id)


def test_delete_uses_post_not_get(authenticated_client, db_session, project_factory, article_factory):
    project, session_id, run_id, message_id = _setup_project_with_question(
        db_session, project_factory, article_factory
    )

    response = authenticated_client.get(_delete_url(project.id, message_id))

    assert response.status_code == 405
    db_session.rollback()
    assert _exists(db_session, ChatMessage, message_id)


def test_delete_nonexistent_message_returns_404(authenticated_client, project_factory):
    project = project_factory()

    response = authenticated_client.post(_delete_url(project.id, uuid.uuid4()))

    assert response.status_code == 404


def test_delete_with_malformed_project_returns_404(
    authenticated_client, db_session, project_factory, article_factory
):
    project, session_id, run_id, message_id = _setup_project_with_question(
        db_session, project_factory, article_factory
    )

    response = authenticated_client.post(_delete_url(uuid.uuid4(), message_id))

    assert response.status_code == 404
    db_session.rollback()
    assert _exists(db_session, ChatMessage, message_id)
