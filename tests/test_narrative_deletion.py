import uuid

from sqlalchemy import select

from app.models.article import Article
from app.models.narrative import NarrativeGeneration, NarrativeInsight
from app.models.project import Project
from app.services.analytics import AnalyticsFilters
from app.services.narrative_service import create_project_generation


def _exists(db_session, model, id_) -> bool:
    """A fresh SELECT, never Session.get() or a live ORM object's `.id`
    after db_session.rollback() -- rollback expires every attribute
    (including primary keys) on every tracked object, so touching `.id` on
    an object whose row has since been deleted (by the request's own,
    different session) raises ObjectDeletedError instead of returning
    None/False. IDs used in assertions here are always captured as plain
    UUIDs before any delete/rollback -- see the test bodies.
    """
    return db_session.execute(select(model).where(model.id == id_)).scalar_one_or_none() is not None


def _delete_url(project_id, generation_id) -> str:
    return f"/projects/{project_id}/narrative-generations/{generation_id}/delete"


def _delete_all_url(project_id) -> str:
    return f"/projects/{project_id}/narratives/delete-all"


def _make_insight(db_session, generation, **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        generation_id=generation.id,
        project_id=generation.project_id,
        narrative_type="executive_summary",
        key="main",
        title="Title",
        narrative="Narrative text.",
        evidence_type="kpi_delta",
        evidence=[],
        raw_candidate={"narrative_type": "executive_summary", "key": "main"},
        validation_status="valid",
    )
    defaults.update(overrides)
    insight = NarrativeInsight(**defaults)
    db_session.add(insight)
    db_session.commit()
    db_session.refresh(insight)
    return insight


def _setup_project_with_generation(db_session, project_factory, article_factory):
    """Returns (project, generation_id, insight_id) -- IDs captured as
    plain UUIDs so later assertions never need to touch a possibly-deleted
    live ORM object's attributes (see _exists's docstring).
    """
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()
    generation, _is_new = create_project_generation(db_session, project, AnalyticsFilters())
    insight = _make_insight(db_session, generation)
    return project, generation.id, insight.id


# --- delete one generation ------------------------------------------------------


def test_delete_one_generation_removes_it_and_redirects_to_insights_tab(
    authenticated_client, db_session, project_factory, article_factory
):
    project, generation_id, insight_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )

    response = authenticated_client.post(
        _delete_url(project.id, generation_id), follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}?tab=insights&insights_deleted=1"

    follow_up = authenticated_client.get(response.headers["location"])
    assert follow_up.status_code == 200
    assert "The narrative generation was deleted." in follow_up.text
    assert "No narrative generations yet." in follow_up.text


def test_dependent_insight_records_are_deleted(
    authenticated_client, db_session, project_factory, article_factory
):
    project, generation_id, insight_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )

    authenticated_client.post(_delete_url(project.id, generation_id))

    db_session.rollback()
    assert not _exists(db_session, NarrativeGeneration, generation_id)
    assert not _exists(db_session, NarrativeInsight, insight_id)


def test_other_generations_in_the_same_project_remain(
    authenticated_client, db_session, project_factory, article_factory
):
    project, first_generation_id, first_insight_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )
    second_generation, _is_new = create_project_generation(
        db_session, project, AnalyticsFilters(), force_regenerate=True
    )
    second_generation_id = second_generation.id
    second_insight = _make_insight(db_session, second_generation)
    second_insight_id = second_insight.id

    authenticated_client.post(_delete_url(project.id, first_generation_id))

    db_session.rollback()
    assert not _exists(db_session, NarrativeGeneration, first_generation_id)
    assert not _exists(db_session, NarrativeInsight, first_insight_id)
    assert _exists(db_session, NarrativeGeneration, second_generation_id)
    assert _exists(db_session, NarrativeInsight, second_insight_id)


def test_generations_in_another_project_remain(
    authenticated_client, db_session, project_factory, article_factory
):
    project_a, generation_a_id, insight_a_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )
    project_b, generation_b_id, insight_b_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )

    authenticated_client.post(_delete_url(project_a.id, generation_a_id))

    db_session.rollback()
    assert not _exists(db_session, NarrativeGeneration, generation_a_id)
    assert _exists(db_session, NarrativeGeneration, generation_b_id)
    assert _exists(db_session, NarrativeInsight, insight_b_id)

    follow_up = authenticated_client.get(f"/projects/{project_b.id}?tab=insights")
    assert follow_up.status_code == 200
    assert "No narrative generations yet." not in follow_up.text


def test_mismatched_project_and_generation_cannot_delete_data(
    authenticated_client, db_session, project_factory, article_factory
):
    project_a, generation_a_id, insight_a_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )
    project_b = project_factory(name="Unrelated Project")

    response = authenticated_client.post(_delete_url(project_b.id, generation_a_id))

    assert response.status_code == 404
    db_session.rollback()
    assert _exists(db_session, NarrativeGeneration, generation_a_id)
    assert _exists(db_session, NarrativeInsight, insight_a_id)


# --- delete all ------------------------------------------------------------------


def test_delete_all_removes_only_insights_for_the_selected_project(
    authenticated_client, db_session, project_factory, article_factory
):
    project_a, generation_a_id, insight_a_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )
    project_b, generation_b_id, insight_b_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )
    project_a_id = project_a.id

    response = authenticated_client.post(_delete_all_url(project_a_id), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project_a_id}?tab=insights&insights_deleted_all=1"

    db_session.rollback()
    assert not _exists(db_session, NarrativeGeneration, generation_a_id)
    assert not _exists(db_session, NarrativeInsight, insight_a_id)

    assert _exists(db_session, NarrativeGeneration, generation_b_id)
    assert _exists(db_session, NarrativeInsight, insight_b_id)
    assert _exists(db_session, Project, project_a_id)


def test_delete_all_does_not_touch_articles_or_project(
    authenticated_client, db_session, project_factory, article_factory
):
    project, generation_id, insight_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )
    project_id = project.id
    article_ids = [a.id for a in project.articles]

    authenticated_client.post(_delete_all_url(project_id))

    db_session.rollback()
    assert _exists(db_session, Project, project_id)
    for article_id in article_ids:
        assert _exists(db_session, Article, article_id)


def test_delete_all_removes_multiple_generations_at_once(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    generation_one, _ = create_project_generation(db_session, project, AnalyticsFilters())
    generation_two, _ = create_project_generation(
        db_session, project, AnalyticsFilters(), force_regenerate=True
    )
    generation_one_id = generation_one.id
    generation_two_id = generation_two.id

    response = authenticated_client.post(_delete_all_url(project.id), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}?tab=insights&insights_deleted_all=2"

    db_session.rollback()
    assert not _exists(db_session, NarrativeGeneration, generation_one_id)
    assert not _exists(db_session, NarrativeGeneration, generation_two_id)


def test_delete_all_empty_state(authenticated_client, db_session, project_factory):
    project = project_factory()

    response = authenticated_client.post(_delete_all_url(project.id), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}?tab=insights&insights_deleted_all=0"

    follow_up = authenticated_client.get(response.headers["location"])
    assert "There were no insights to delete." in follow_up.text


def test_insights_tab_hides_delete_all_when_there_is_nothing_to_delete(
    authenticated_client, db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    project.valid_rows = 1
    db_session.commit()

    response = authenticated_client.get(f"/projects/{project.id}?tab=insights")

    assert response.status_code == 200
    assert "No narrative generations yet." in response.text
    assert "Delete all insights" not in response.text


def test_insights_tab_shows_delete_all_when_a_generation_exists(
    authenticated_client, db_session, project_factory, article_factory
):
    project, generation_id, insight_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )

    response = authenticated_client.get(f"/projects/{project.id}?tab=insights")

    assert response.status_code == 200
    assert "Delete all insights" in response.text


# --- authentication ----------------------------------------------------------------


def test_unauthenticated_single_delete_is_redirected_to_login(
    client, db_session, project_factory, article_factory
):
    project, generation_id, insight_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )

    response = client.post(_delete_url(project.id, generation_id), follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"

    db_session.rollback()
    assert _exists(db_session, NarrativeGeneration, generation_id)


def test_unauthenticated_delete_all_is_redirected_to_login(
    client, db_session, project_factory, article_factory
):
    project, generation_id, insight_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )

    response = client.post(_delete_all_url(project.id), follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"

    db_session.rollback()
    assert _exists(db_session, NarrativeGeneration, generation_id)


def test_delete_uses_post_not_get(authenticated_client, db_session, project_factory, article_factory):
    project, generation_id, insight_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )

    response = authenticated_client.get(_delete_url(project.id, generation_id))

    assert response.status_code == 405
    db_session.rollback()
    assert _exists(db_session, NarrativeGeneration, generation_id)


def test_delete_nonexistent_generation_returns_404(authenticated_client, project_factory):
    project = project_factory()

    response = authenticated_client.post(_delete_url(project.id, uuid.uuid4()))

    assert response.status_code == 404


def test_delete_with_malformed_project_returns_404(
    authenticated_client, db_session, project_factory, article_factory
):
    project, generation_id, insight_id = _setup_project_with_generation(
        db_session, project_factory, article_factory
    )

    response = authenticated_client.post(_delete_url(uuid.uuid4(), generation_id))

    assert response.status_code == 404
    db_session.rollback()
    assert _exists(db_session, NarrativeGeneration, generation_id)
