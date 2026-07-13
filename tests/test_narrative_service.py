import copy

import pytest

from app.models.narrative import NarrativeGeneration, NarrativeGenerationStatus
from app.schemas.narrative import NarrativeResultsSubmission
from app.services.analytics import AnalyticsFilters
from app.services.narrative_contract import PAYLOAD_SCHEMA_VERSION, NarrativeTypes
from app.services.narrative_service import (
    NarrativeServiceError,
    create_comparison_generation,
    create_project_generation,
    get_project_narrative_generations,
    process_results,
)


def _valid_candidate(**overrides) -> dict:
    base = {
        "narrative_type": "executive_summary",
        "key": "main",
        "title": "Title",
        "narrative": "Narrative text.",
        "evidence_type": "kpi_delta",
        "evidence": [],
        "related_article_ids": [],
        "source_urls": [],
    }
    base.update(overrides)
    return base


def test_create_project_generation_applies_scope_defaults(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    generation, is_new = create_project_generation(db_session, project, AnalyticsFilters())

    assert is_new
    assert generation.status == NarrativeGenerationStatus.PENDING
    assert set(generation.narrative_types) == set(NarrativeTypes.PROJECT_SCOPE_DEFAULTS)
    assert generation.baseline_project_ids is None
    assert generation.prompt_contract_version
    assert generation.payload_schema_version
    assert generation.validator_version


def test_create_project_generation_rejects_comparison_only_type(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    with pytest.raises(NarrativeServiceError) as exc_info:
        create_project_generation(
            db_session, project, AnalyticsFilters(), narrative_types=["comparison_executive_summary"]
        )
    assert exc_info.value.status_code == 422


def test_create_comparison_generation_applies_scope_defaults(
    db_session, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    generation, is_new = create_comparison_generation(db_session, [a.id], [b.id], AnalyticsFilters())

    assert is_new
    assert set(generation.narrative_types) == set(NarrativeTypes.COMPARISON_SCOPE_DEFAULTS)
    assert generation.baseline_project_ids == [str(a.id)]
    assert generation.comparison_project_ids == [str(b.id)]
    # Anchor project only — never affects analytical scope.
    assert generation.project_id == a.id


def test_create_comparison_generation_rejects_project_only_type(
    db_session, project_factory, article_factory
):
    a = project_factory(name="A", quarter="2026-Q1")
    b = project_factory(name="B", quarter="2026-Q2")
    article_factory(a, count=1, retailer="Auchan")
    article_factory(b, count=1, retailer="Auchan")

    with pytest.raises(NarrativeServiceError) as exc_info:
        create_comparison_generation(
            db_session, [a.id], [b.id], AnalyticsFilters(), narrative_types=["executive_summary"]
        )
    assert exc_info.value.status_code == 422


def test_dedup_reuses_complete_generation_for_unchanged_input(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    generation.status = NarrativeGenerationStatus.COMPLETE
    db_session.commit()

    reused, is_new = create_project_generation(db_session, project, AnalyticsFilters())

    assert not is_new
    assert reused.id == generation.id


def test_source_snapshot_immutable_after_project_data_changes(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    original_snapshot = copy.deepcopy(generation.source_snapshot)
    assert original_snapshot["data"]["kpis"]["unique_valid_articles"] == 1

    # Data changes after the generation was created.
    article_factory(project, count=5, retailer="Carrefour")

    db_session.refresh(generation)
    assert generation.source_snapshot == original_snapshot
    assert generation.source_snapshot["data"]["kpis"]["unique_valid_articles"] == 1


def test_force_regenerate_creates_new_generation_with_lineage(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    first, _ = create_project_generation(db_session, project, AnalyticsFilters())
    first.status = NarrativeGenerationStatus.COMPLETE
    db_session.commit()

    second, is_new = create_project_generation(
        db_session, project, AnalyticsFilters(), force_regenerate=True
    )

    assert is_new
    assert second.id != first.id
    assert second.regenerated_from_generation_id == first.id

    # The prior generation is untouched and still separately retrievable.
    db_session.refresh(first)
    assert first.status == NarrativeGenerationStatus.COMPLETE

    all_generations = get_project_narrative_generations(db_session, project.id)
    assert {g.id for g in all_generations} == {first.id, second.id}


def test_process_results_partially_complete(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    generation, _ = create_project_generation(
        db_session,
        project,
        AnalyticsFilters(),
        narrative_types=["executive_summary", "key_findings"],
    )
    unique_valid = generation.source_snapshot["data"]["kpis"]["unique_valid_articles"]

    submission = NarrativeResultsSubmission(
        model="deepseek-chat",
        prompt_version="v1",
        payload_schema_version=generation.payload_schema_version,
        insights=[
            _valid_candidate(
                narrative_type="executive_summary",
                evidence=[
                    {"path": "kpis.unique_valid_articles", "role": "value", "value": unique_valid}
                ],
            ),
            _valid_candidate(
                narrative_type="key_findings",
                key="bad",
                evidence=[{"path": "kpis.unique_valid_articles", "role": "value", "value": 999999}],
            ),
        ],
    )

    updated = process_results(db_session, generation, submission)

    assert updated.status == NarrativeGenerationStatus.PARTIALLY_COMPLETE
    assert updated.missing_narrative_types == ["key_findings"]

    valid = [i for i in updated.insights if i.validation_status == "valid"]
    rejected = [i for i in updated.insights if i.validation_status == "rejected"]
    assert len(valid) == 1
    assert len(rejected) == 1
    assert rejected[0].rejection_reason is not None


def test_process_results_failed_when_all_rejected(db_session, project_factory, article_factory):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    generation, _ = create_project_generation(
        db_session, project, AnalyticsFilters(), narrative_types=["executive_summary"]
    )

    submission = NarrativeResultsSubmission(
        model="deepseek-chat",
        prompt_version="v1",
        payload_schema_version=generation.payload_schema_version,
        insights=[
            _valid_candidate(
                evidence=[{"path": "kpis.unique_valid_articles", "role": "value", "value": 999999}]
            )
        ],
    )

    updated = process_results(db_session, generation, submission)
    assert updated.status == NarrativeGenerationStatus.FAILED
    assert updated.missing_narrative_types == ["executive_summary"]


def test_process_results_malformed_candidate_does_not_fail_batch(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    generation, _ = create_project_generation(
        db_session, project, AnalyticsFilters(), narrative_types=["executive_summary"]
    )
    unique_valid = generation.source_snapshot["data"]["kpis"]["unique_valid_articles"]

    submission = NarrativeResultsSubmission(
        model="deepseek-chat",
        prompt_version="v1",
        payload_schema_version=generation.payload_schema_version,
        insights=[
            {"narrative_type": "executive_summary"},  # missing required fields
            _valid_candidate(
                evidence=[
                    {"path": "kpis.unique_valid_articles", "role": "value", "value": unique_valid}
                ]
            ),
        ],
    )

    updated = process_results(db_session, generation, submission)
    assert updated.status == NarrativeGenerationStatus.COMPLETE

    malformed = [i for i in updated.insights if i.title is None]
    assert len(malformed) == 1
    assert malformed[0].validation_status == "rejected"
    assert malformed[0].raw_candidate == {"narrative_type": "executive_summary"}


def test_process_results_rejects_mismatched_payload_schema_version(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())

    submission = NarrativeResultsSubmission(
        model="deepseek-chat",
        prompt_version="v1",
        payload_schema_version="some-other-version",
        insights=[],
    )

    with pytest.raises(NarrativeServiceError) as exc_info:
        process_results(db_session, generation, submission)
    assert exc_info.value.status_code == 422


def test_process_results_stores_app_controlled_contract_versions_unaffected_by_submission(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    generation, _ = create_project_generation(db_session, project, AnalyticsFilters())
    original_contract_version = generation.prompt_contract_version
    original_validator_version = generation.validator_version

    submission = NarrativeResultsSubmission(
        model="some-other-model",
        prompt_version="some-other-prompt-version",
        payload_schema_version=generation.payload_schema_version,
        insights=[],
    )
    updated = process_results(db_session, generation, submission)

    assert updated.model == "some-other-model"
    assert updated.prompt_version == "some-other-prompt-version"
    # App-controlled fields are untouched by whatever n8n reports.
    assert updated.prompt_contract_version == original_contract_version
    assert updated.validator_version == original_validator_version


def test_rejected_insights_excluded_from_relationship_filtering(
    db_session, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")

    generation, _ = create_project_generation(
        db_session, project, AnalyticsFilters(), narrative_types=["executive_summary"]
    )
    submission = NarrativeResultsSubmission(
        model="deepseek-chat",
        prompt_version="v1",
        payload_schema_version=generation.payload_schema_version,
        insights=[_valid_candidate(evidence=[{"path": "kpis.nope", "role": "value", "value": 1}])],
    )
    updated = process_results(db_session, generation, submission)

    valid_ids = {i.id for i in updated.insights if i.validation_status == "valid"}
    assert valid_ids == set()


def test_create_project_generation_does_not_crash_with_uploaded_file_ids(
    db_session, project_factory, article_factory
):
    """Narrative generation writes used to call raw dataclasses.asdict on
    the filters, which does not stringify uuid.UUID -- crashed for any
    non-empty uploaded_file_ids. Must now use the canonical serializer.
    """
    import uuid

    project = project_factory()
    article_factory(project, count=1, retailer="Auchan")
    filters = AnalyticsFilters(uploaded_file_ids=(uuid.uuid4(),))

    generation, is_new = create_project_generation(db_session, project, filters=filters)

    assert is_new is True
    assert generation.filters == {"source_files": [str(u) for u in filters.uploaded_file_ids]}
