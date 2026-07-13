import pytest

from app.models.chat import ChatMessage, ChatRun
from app.schemas.chat import AnswerSubmission, PlanSubmission
from app.services.analytics import AnalyticsFilters
from app.services.chat_contract import PROMPT_CONTRACT_VERSION
from app.services.chat_service import (
    ChatServiceError,
    compute_scope_key,
    create_run,
    find_or_create_comparison_session,
    find_or_create_project_session,
    process_answer,
    process_plan,
    retry_run,
)


def _plan(run, brand="Auchan"):
    return PlanSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        tool_calls=[{"tool": "get_brand_performance", "parameters": {"brand": brand}}],
    )


def _valid_answer(run, sov, brand="Auchan"):
    return AnswerSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        answer_text=f"{brand} a avut un SOV de {sov}%.", answer_type="fact",
        evidence=[{"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": sov}],
        related_brand=brand,
    )


def _bad_answer(run):
    return AnswerSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        answer_text="A avut 999999 de articole.", answer_type="fact",
        evidence=[{"kind": "metric", "tool_call_index": 0, "path": "requested_brand.sov_pct", "role": "value", "value": 0.1}],
    )


# --- scope_key canonicalization -----------------------------------------------


def test_scope_key_independent_of_baseline_order(project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    filters = AnalyticsFilters()

    key1 = compute_scope_key("comparison", None, [a.id, b.id], [b.id], filters, "ro")
    key2 = compute_scope_key("comparison", None, [b.id, a.id], [b.id], filters, "ro")
    assert key1 == key2


def test_scope_key_deduplicates_ids(project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    filters = AnalyticsFilters()

    key1 = compute_scope_key("comparison", None, [a.id], [a.id], filters, "ro")
    key2 = compute_scope_key("comparison", None, [a.id, a.id], [a.id], filters, "ro")
    assert key1 == key2


def test_scope_key_differs_by_language(project_factory):
    project = project_factory()
    filters = AnalyticsFilters()

    key_ro = compute_scope_key("project", project.id, None, None, filters, "ro")
    key_en = compute_scope_key("project", project.id, None, None, filters, "en")
    assert key_ro != key_en


# --- Concurrency-safe find-or-create --------------------------------------------


def test_find_or_create_project_session_is_idempotent(db_session, project_factory):
    project = project_factory()
    session1 = find_or_create_project_session(db_session, project)
    session2 = find_or_create_project_session(db_session, project)
    assert session1.id == session2.id


def test_find_or_create_comparison_session_is_idempotent(db_session, project_factory):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    c = project_factory(name="C", quarter="2026-Q3")
    session1 = find_or_create_comparison_session(db_session, [a.id, b.id], [c.id])
    # Same baseline set, different submission order within that side.
    session2 = find_or_create_comparison_session(db_session, [b.id, a.id], [c.id])
    assert session1.id == session2.id


def test_comparison_session_requires_both_sides(db_session, project_factory):
    a = project_factory()
    with pytest.raises(ChatServiceError) as exc_info:
        find_or_create_comparison_session(db_session, [a.id], [])
    assert exc_info.value.status_code == 422


# --- Run creation and concurrency guard -----------------------------------------


def test_create_run_persists_planning_snapshot(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)

    run = create_run(db_session, session, "Care este SOV pentru Auchan?")

    assert run.status == "pending"
    assert run.planning_payload_snapshot["question"] == "Care este SOV pentru Auchan?"
    assert run.prompt_contract_version == PROMPT_CONTRACT_VERSION


def test_create_run_rejects_empty_question(db_session, project_factory):
    project = project_factory()
    session = find_or_create_project_session(db_session, project)
    with pytest.raises(ChatServiceError) as exc_info:
        create_run(db_session, session, "   ")
    assert exc_info.value.status_code == 422


def test_create_run_rejects_oversized_question(db_session, project_factory):
    project = project_factory()
    session = find_or_create_project_session(db_session, project)
    with pytest.raises(ChatServiceError) as exc_info:
        create_run(db_session, session, "a" * 2001)
    assert exc_info.value.status_code == 422


def test_create_run_rejects_second_active_run_in_same_session(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    create_run(db_session, session, "First question?")

    with pytest.raises(ChatServiceError) as exc_info:
        create_run(db_session, session, "Second question?")
    assert exc_info.value.status_code == 409

    # No orphan second user message was created.
    messages = db_session.query(ChatMessage).filter_by(session_id=session.id).all()
    assert len(messages) == 1


# --- Strict state transitions and idempotency -----------------------------------


def test_plan_success_transitions_pending_to_running(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Care este SOV pentru Auchan?")

    outcome = process_plan(db_session, run, _plan(run))

    assert outcome.http_status == 200
    assert run.status == "running"


def test_plan_failure_transitions_pending_to_failed(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")

    bad_plan = PlanSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        tool_calls=[{"tool": "not_a_real_tool", "parameters": {}}],
    )
    outcome = process_plan(db_session, run, bad_plan)

    assert outcome.http_status == 422
    assert run.status == "failed"
    assert run.error_message is not None


def test_identical_plan_retry_replays_success_without_reexecuting(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")
    plan = _plan(run)

    first = process_plan(db_session, run, plan)
    second = process_plan(db_session, run, plan)

    assert first.http_status == 200
    assert second.http_status == 200
    assert second.tool_results == first.tool_results


def test_identical_plan_retry_after_failure_replays_422_not_200(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")

    bad_plan = PlanSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        tool_calls=[{"tool": "not_a_real_tool", "parameters": {}}],
    )
    first = process_plan(db_session, run, bad_plan)
    second = process_plan(db_session, run, bad_plan)

    assert first.http_status == 422
    assert second.http_status == 422, "an identical retry of a failed plan must not become a 200"
    assert second.error_detail == first.error_detail


def test_conflicting_plan_resubmission_rejected(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")

    process_plan(db_session, run, _plan(run, brand="Auchan"))
    conflicting = process_plan(db_session, run, _plan(run, brand="NotSubmittedBefore"))

    assert conflicting.http_status == 409


def test_answer_before_plan_rejected(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")

    fake_answer = AnswerSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        answer_text="Text.", answer_type="fact",
        evidence=[{"kind": "metric", "tool_call_index": 0, "path": "x", "role": "value", "value": 1}],
    )
    outcome = process_answer(db_session, run, fake_answer)
    assert outcome.http_status == 409


def test_valid_answer_creates_assistant_message(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")
    plan_outcome = process_plan(db_session, run, _plan(run))
    sov = plan_outcome.tool_results[0]["requested_brand"]["sov_pct"]

    outcome = process_answer(db_session, run, _valid_answer(run, sov))

    assert outcome.http_status == 200
    assert run.status == "complete"
    messages = db_session.query(ChatMessage).filter_by(session_id=session.id).order_by(ChatMessage.created_at).all()
    assert [m.role for m in messages] == ["user", "assistant"]


def test_rejected_answer_does_not_create_assistant_message(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")
    process_plan(db_session, run, _plan(run))

    outcome = process_answer(db_session, run, _bad_answer(run))

    assert outcome.http_status == 422
    assert run.status == "failed"
    messages = db_session.query(ChatMessage).filter_by(session_id=session.id).all()
    assert [m.role for m in messages] == ["user"]
    # Audit trail still records the rejected content on the run itself.
    assert run.answer_text is not None
    assert run.rejection_reason is not None


def test_identical_answer_retry_after_rejection_replays_422_not_200(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")
    process_plan(db_session, run, _plan(run))
    bad_answer = _bad_answer(run)

    first = process_answer(db_session, run, bad_answer)
    second = process_answer(db_session, run, bad_answer)

    assert first.http_status == 422
    assert second.http_status == 422, "an identical retry of a rejected answer must not become a 200"
    assert second.rejection_reason == first.rejection_reason

    messages = db_session.query(ChatMessage).filter_by(session_id=session.id).all()
    assert [m.role for m in messages] == ["user"], "rejected retry must not create a message either"


def test_conflicting_answer_resubmission_rejected(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")
    plan_outcome = process_plan(db_session, run, _plan(run))
    sov = plan_outcome.tool_results[0]["requested_brand"]["sov_pct"]

    process_answer(db_session, run, _valid_answer(run, sov))
    conflicting = process_answer(db_session, run, _bad_answer(run))

    assert conflicting.http_status == 409


def test_identical_answer_retry_after_success_replays_200(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")
    plan_outcome = process_plan(db_session, run, _plan(run))
    sov = plan_outcome.tool_results[0]["requested_brand"]["sov_pct"]
    answer = _valid_answer(run, sov)

    first = process_answer(db_session, run, answer)
    second = process_answer(db_session, run, answer)

    assert first.http_status == 200
    assert second.http_status == 200
    # No duplicate assistant message from the replayed request.
    messages = db_session.query(ChatMessage).filter_by(session_id=session.id).all()
    assert [m.role for m in messages] == ["user", "assistant"]


def test_app_controlled_versions_unaffected_by_submission(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")
    original_contract_version = run.prompt_contract_version
    process_plan(db_session, run, _plan(run))

    submission = PlanSubmission(
        model="some-other-model", prompt_version="some-other-version",
        payload_schema_version=run.payload_schema_version,
        tool_calls=[{"tool": "get_brand_performance", "parameters": {"brand": "Auchan"}}],
    )
    # Re-submitting an identical (already-processed) plan is idempotent and
    # doesn't change stored model/prompt_version either way here; assert the
    # contract version specifically is immune regardless.
    process_plan(db_session, run, submission)
    assert run.prompt_contract_version == original_contract_version


# --- Retry lineage ---------------------------------------------------------------


def test_retry_only_allowed_from_failed(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")

    with pytest.raises(ChatServiceError) as exc_info:
        retry_run(db_session, run)
    assert exc_info.value.status_code == 409


def test_retry_creates_new_run_reusing_same_user_message(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)
    run = create_run(db_session, session, "Q?")
    bad_plan = PlanSubmission(
        model="m", prompt_version="v1", payload_schema_version=run.payload_schema_version,
        tool_calls=[{"tool": "not_a_real_tool", "parameters": {}}],
    )
    process_plan(db_session, run, bad_plan)
    assert run.status == "failed"

    new_run = retry_run(db_session, run)

    assert new_run.id != run.id
    assert new_run.status == "pending"
    assert new_run.retry_of_run_id == run.id
    assert new_run.user_message_id == run.user_message_id

    all_runs = db_session.query(ChatRun).filter_by(session_id=session.id).all()
    assert len(all_runs) == 2
    messages = db_session.query(ChatMessage).filter_by(session_id=session.id).all()
    assert len(messages) == 1, "retry must not create a duplicate question message"


# --- Stale-source non-reuse -------------------------------------------------------


def test_second_run_reflects_fresh_data_not_first_runs_snapshot(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)

    run1 = create_run(db_session, session, "First question?")
    outcome1 = process_plan(db_session, run1, _plan(run1))
    sov1 = outcome1.tool_results[0]["requested_brand"]["sov_pct"]
    process_answer(db_session, run1, _valid_answer(run1, sov1))

    # Data changes between questions.
    article_factory(project, count=9, retailer="Carrefour")

    run2 = create_run(db_session, session, "Second question?")
    outcome2 = process_plan(db_session, run2, _plan(run2))
    sov2 = outcome2.tool_results[0]["requested_brand"]["sov_pct"]

    assert sov1 != sov2, "the second run must reflect the newly-added competitor data"
    # The first run's persisted snapshot is untouched.
    db_session.refresh(run1)
    assert run1.answer_payload_snapshot["tool_results"][0]["requested_brand"]["sov_pct"] == sov1


# --- Conversation history persistence --------------------------------------------


def test_conversation_history_includes_prior_valid_turn(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    session = find_or_create_project_session(db_session, project)

    run1 = create_run(db_session, session, "Prima intrebare?")
    outcome1 = process_plan(db_session, run1, _plan(run1))
    sov1 = outcome1.tool_results[0]["requested_brand"]["sov_pct"]
    process_answer(db_session, run1, _valid_answer(run1, sov1))

    run2 = create_run(db_session, session, "A doua intrebare?")

    history = run2.planning_payload_snapshot["conversation_history"]
    assert {"role": "user", "content": "Prima intrebare?"} in history
    assert any(h["role"] == "assistant" for h in history)


# --- Phase D corrections: crash fix, read compatibility, legacy fallback ----


def test_compute_scope_key_does_not_crash_with_uploaded_file_ids(project_factory):
    """`compute_scope_key` used to call raw `dataclasses.asdict(filters)`,
    which does not stringify `uuid.UUID` -- any request with a non-empty
    `uploaded_file_ids` crashed. Must now use the canonical serializer.
    """
    import uuid

    project = project_factory()
    filters = AnalyticsFilters(uploaded_file_ids=(uuid.uuid4(),))
    key = compute_scope_key("project", project.id, None, None, filters, "ro")
    assert isinstance(key, str) and key


def test_find_or_create_project_session_does_not_crash_with_uploaded_file_ids(
    db_session, project_factory
):
    import uuid

    project = project_factory()
    filters = AnalyticsFilters(uploaded_file_ids=(uuid.uuid4(),))
    session = find_or_create_project_session(db_session, project, filters)
    assert session.filters == {"source_files": [str(u) for u in filters.uploaded_file_ids]}


def test_build_scope_context_round_trips_new_shaped_stored_json(db_session, project_factory):
    import uuid

    from app.services.chat_tools import build_scope_context

    project = project_factory()
    file_id = uuid.uuid4()
    filters = AnalyticsFilters(brands=("Auchan",), uploaded_file_ids=(file_id,), include_needs_review=True)
    session = find_or_create_project_session(db_session, project, filters)

    scope = build_scope_context(db_session, session)
    assert scope.filters.brands == ("Auchan",)
    assert scope.filters.uploaded_file_ids == (file_id,)
    assert scope.filters.include_needs_review is True


def test_build_scope_context_round_trips_old_phase_c_shaped_stored_json(db_session, project_factory):
    """A ChatSession created before this correction stored the Phase C
    six-field shape directly (via asdict) -- must still read back
    correctly through the now-canonical parser.
    """
    from app.models.chat import ChatSession
    from app.services.chat_tools import build_scope_context

    project = project_factory()
    old_shaped_filters = {
        "brand": "Auchan", "publication": None, "primary_topic": None,
        "communication_category": None, "sentiment": None, "state": "all",
    }
    session = ChatSession(
        project_id=project.id,
        filters=old_shaped_filters,
        language="ro",
        scope_key="legacy-shape-probe-key",
    )
    db_session.add(session)
    db_session.commit()

    scope = build_scope_context(db_session, session)
    assert scope.filters.brand == "Auchan"
    assert scope.filters.brands == ("Auchan",)
    assert scope.filters.uploaded_file_ids == ()


def test_build_scope_context_round_trips_interim_phase_d_shaped_stored_json(
    db_session, project_factory
):
    """A ChatSession written by the current, not-yet-corrected Phase D code
    (pre this fix) used the interim key names `uploaded_file_ids`/
    `include_needs_review` -- must still read back correctly.
    """
    import uuid

    from app.models.chat import ChatSession
    from app.services.chat_tools import build_scope_context

    project = project_factory()
    file_id = uuid.uuid4()
    interim_shaped_filters = {
        "brand": None, "brands": ["Auchan"], "uploaded_file_ids": [str(file_id)],
        "include_needs_review": True, "publication": None, "primary_topic": None,
        "communication_category": None, "sentiment": None, "state": "all",
    }
    session = ChatSession(
        project_id=project.id,
        filters=interim_shaped_filters,
        language="ro",
        scope_key="interim-shape-probe-key",
    )
    db_session.add(session)
    db_session.commit()

    scope = build_scope_context(db_session, session)
    assert scope.filters.brands == ("Auchan",)
    assert scope.filters.uploaded_file_ids == (file_id,)
    assert scope.filters.include_needs_review is True


def test_legacy_phase_c_scope_key_is_still_reused(db_session, project_factory):
    """A ChatSession created before this correction, whose scope_key was
    computed from the exact historical Phase C payload shape, must still
    be found and reused by a new request for the logically-equivalent
    filters -- not silently duplicated into a second session.
    """
    from app.models.chat import ChatSession
    from app.services.json_safe import hash_json

    project = project_factory()
    legacy_filters_payload = {
        "brand": "Auchan", "publication": None, "primary_topic": None,
        "communication_category": None, "sentiment": None, "state": "all",
    }
    legacy_payload = {
        "kind": "project",
        "project_id": str(project.id),
        "baseline_project_ids": None,
        "comparison_project_ids": None,
        "filters": legacy_filters_payload,
        "language": "ro",
    }
    legacy_scope_key = hash_json(legacy_payload)
    old_session = ChatSession(
        project_id=project.id,
        filters=legacy_filters_payload,
        language="ro",
        scope_key=legacy_scope_key,
    )
    db_session.add(old_session)
    db_session.commit()

    found = find_or_create_project_session(db_session, project, AnalyticsFilters(brand="Auchan"))
    assert found.id == old_session.id
