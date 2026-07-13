"""Orchestrates narrative-generation creation, dedup/regeneration lineage,
and results processing.

Mirrors the existing classification batch/n8n round-trip pattern
(`app/services/classification.py`, `app/api/classification.py`): a browser
route creates a `pending` `NarrativeGeneration` (or reuses a matching
`complete`/`partially_complete` one) and triggers n8n; n8n later calls back
into the internal API to fetch the persisted payload and submit results,
which `process_results` here validates and persists.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.narrative import (
    NarrativeGeneration,
    NarrativeGenerationStatus,
    NarrativeInsight,
    NarrativeValidationStatus,
)
from app.models.project import Project
from app.schemas.narrative import NarrativeResultsSubmission
from app.services.analytics import DEFAULT_TOP_N, AnalyticsFilters, serialize_analytics_filters
from app.services.comparison import ComparisonServiceError
from app.services.narrative_contract import (
    PAYLOAD_SCHEMA_VERSION,
    PROMPT_CONTRACT_VERSION,
    VALIDATOR_VERSION,
    NarrativeTypes,
)
from app.services.narrative_payload import (
    build_comparison_snapshot,
    build_project_snapshot,
    compute_input_hash,
)
from app.services.narrative_validation import compute_generation_outcome, validate_candidate


class NarrativeServiceError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _find_reusable_generation(
    db: Session, project_id: uuid.UUID, input_hash: str
) -> NarrativeGeneration | None:
    stmt = (
        select(NarrativeGeneration)
        .where(
            NarrativeGeneration.project_id == project_id,
            NarrativeGeneration.input_hash == input_hash,
            NarrativeGeneration.status.in_(NarrativeGenerationStatus.REUSABLE),
        )
        .order_by(NarrativeGeneration.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def create_project_generation(
    db: Session,
    project: Project,
    filters: AnalyticsFilters | None = None,
    narrative_types: list[str] | None = None,
    language: str = "ro",
    top_n: int = DEFAULT_TOP_N,
    force_regenerate: bool = False,
) -> tuple[NarrativeGeneration, bool]:
    """Returns (generation, is_new). When `is_new` is False, the caller
    should not trigger n8n — an existing complete/partially_complete
    generation for the same input was reused.
    """
    filters = filters or AnalyticsFilters()
    requested_types = list(narrative_types) if narrative_types else list(
        NarrativeTypes.PROJECT_SCOPE_DEFAULTS
    )
    invalid = [t for t in requested_types if t not in NarrativeTypes.PROJECT_SCOPE_VALID]
    if invalid:
        raise NarrativeServiceError(
            f"Narrative type(s) not valid for a project-scoped generation: "
            f"{', '.join(invalid)}.",
            422,
        )

    snapshot = build_project_snapshot(db, project, filters, top_n=top_n)
    input_hash = compute_input_hash(
        snapshot, language, requested_types, PROMPT_CONTRACT_VERSION, filters
    )

    prior = _find_reusable_generation(db, project.id, input_hash)
    if not force_regenerate and prior is not None:
        return prior, False

    generation = NarrativeGeneration(
        project_id=project.id,
        narrative_types=requested_types,
        baseline_project_ids=None,
        comparison_project_ids=None,
        filters=serialize_analytics_filters(filters),
        source_snapshot=snapshot,
        language=language,
        status=NarrativeGenerationStatus.PENDING,
        missing_narrative_types=None,
        prompt_contract_version=PROMPT_CONTRACT_VERSION,
        payload_schema_version=PAYLOAD_SCHEMA_VERSION,
        validator_version=VALIDATOR_VERSION,
        input_hash=input_hash,
        regenerated_from_generation_id=prior.id if (force_regenerate and prior) else None,
    )
    db.add(generation)
    db.commit()
    db.refresh(generation)
    return generation, True


def create_comparison_generation(
    db: Session,
    baseline_project_ids: list[uuid.UUID],
    comparison_project_ids: list[uuid.UUID],
    filters: AnalyticsFilters | None = None,
    narrative_types: list[str] | None = None,
    language: str = "ro",
    top_n: int = DEFAULT_TOP_N,
    force_regenerate: bool = False,
    baseline_filters: AnalyticsFilters | None = None,
    comparison_filters: AnalyticsFilters | None = None,
) -> tuple[NarrativeGeneration, bool]:
    """`baseline_filters`/`comparison_filters` (Phase E) each default to
    `filters` when omitted -- every existing call site unaffected.
    """
    filters = filters or AnalyticsFilters()
    effective_baseline_filters = baseline_filters or filters
    effective_comparison_filters = comparison_filters or filters
    requested_types = list(narrative_types) if narrative_types else list(
        NarrativeTypes.COMPARISON_SCOPE_DEFAULTS
    )
    invalid = [t for t in requested_types if t not in NarrativeTypes.COMPARISON_SCOPE_VALID]
    if invalid:
        raise NarrativeServiceError(
            f"Narrative type(s) not valid for a comparison-scoped generation: "
            f"{', '.join(invalid)}.",
            422,
        )

    unique_baseline_ids = list(dict.fromkeys(baseline_project_ids))
    unique_comparison_ids = list(dict.fromkeys(comparison_project_ids))
    if not unique_baseline_ids or not unique_comparison_ids:
        raise NarrativeServiceError(
            "Both baseline and comparison project selections are required.", 422
        )

    try:
        snapshot = build_comparison_snapshot(
            db, unique_baseline_ids, unique_comparison_ids, filters, top_n=top_n,
            baseline_filters=effective_baseline_filters, comparison_filters=effective_comparison_filters,
        )
    except ComparisonServiceError as exc:
        raise NarrativeServiceError(exc.message, exc.status_code) from exc

    input_hash = compute_input_hash(
        snapshot, language, requested_types, PROMPT_CONTRACT_VERSION,
        effective_baseline_filters, effective_comparison_filters,
    )

    # Ownership/navigation anchor only (see app/models/narrative.py) — never
    # used for scope, analytics, or validation, which are driven entirely by
    # baseline/comparison_project_ids above.
    anchor_project_id = unique_baseline_ids[0]

    prior = _find_reusable_generation(db, anchor_project_id, input_hash)
    if not force_regenerate and prior is not None:
        return prior, False

    stored_filters = (
        serialize_analytics_filters(filters)
        if effective_baseline_filters == effective_comparison_filters
        else {
            "baseline": serialize_analytics_filters(effective_baseline_filters),
            "comparison": serialize_analytics_filters(effective_comparison_filters),
        }
    )
    generation = NarrativeGeneration(
        project_id=anchor_project_id,
        narrative_types=requested_types,
        baseline_project_ids=[str(pid) for pid in unique_baseline_ids],
        comparison_project_ids=[str(pid) for pid in unique_comparison_ids],
        filters=stored_filters,
        source_snapshot=snapshot,
        language=language,
        status=NarrativeGenerationStatus.PENDING,
        missing_narrative_types=None,
        prompt_contract_version=PROMPT_CONTRACT_VERSION,
        payload_schema_version=PAYLOAD_SCHEMA_VERSION,
        validator_version=VALIDATOR_VERSION,
        input_hash=input_hash,
        regenerated_from_generation_id=prior.id if (force_regenerate and prior) else None,
    )
    db.add(generation)
    db.commit()
    db.refresh(generation)
    return generation, True


def process_results(
    db: Session, generation: NarrativeGeneration, submission: NarrativeResultsSubmission
) -> NarrativeGeneration:
    if submission.payload_schema_version != generation.payload_schema_version:
        raise NarrativeServiceError(
            "payload_schema_version does not match this generation's persisted contract.",
            422,
        )

    scope = "comparison" if generation.baseline_project_ids else "project"
    seen_keys: set[tuple[str, str]] = set()
    valid_narrative_types: set[str] = set()

    for raw in submission.insights:
        result, candidate = validate_candidate(
            raw, generation.source_snapshot, scope, seen_keys
        )

        insight = NarrativeInsight(
            generation_id=generation.id,
            project_id=generation.project_id,
            narrative_type=candidate.narrative_type if candidate else None,
            key=candidate.key if candidate else None,
            title=candidate.title if candidate else None,
            narrative=candidate.narrative if candidate else None,
            evidence_type=candidate.evidence_type if candidate else None,
            evidence=[e.model_dump() for e in candidate.evidence] if candidate else None,
            baseline_value=result.baseline_value,
            comparison_value=result.comparison_value,
            delta=result.delta,
            related_brand=candidate.related_brand if candidate else None,
            related_topic=candidate.related_topic if candidate else None,
            related_publication=candidate.related_publication if candidate else None,
            related_story_key=candidate.related_story_key if candidate else None,
            related_article_ids=[str(a) for a in candidate.related_article_ids]
            if candidate
            else None,
            source_urls=candidate.source_urls if candidate else None,
            confidence=candidate.confidence if candidate else None,
            caveat=candidate.caveat if candidate else None,
            raw_candidate=raw,
            validation_status=(
                NarrativeValidationStatus.VALID if result.valid else NarrativeValidationStatus.REJECTED
            ),
            rejection_reason=None if result.valid else result.reason,
        )
        db.add(insight)

        if result.valid and candidate is not None:
            seen_keys.add((candidate.narrative_type, candidate.key))
            valid_narrative_types.add(candidate.narrative_type)

    status, missing = compute_generation_outcome(
        generation.narrative_types, valid_narrative_types
    )

    generation.model = submission.model
    generation.prompt_version = submission.prompt_version
    generation.status = status
    generation.missing_narrative_types = missing
    generation.completed_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(generation)
    return generation


def get_project_narrative_generations(
    db: Session, project_id: uuid.UUID
) -> list[NarrativeGeneration]:
    stmt = (
        select(NarrativeGeneration)
        .where(NarrativeGeneration.project_id == project_id)
        .order_by(NarrativeGeneration.created_at.desc())
    )
    return list(db.execute(stmt).scalars().all())
