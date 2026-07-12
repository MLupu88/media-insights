import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.narrative import NarrativeGeneration, NarrativeInsight, NarrativeValidationStatus
from app.models.project import Project
from app.schemas.narrative import (
    NarrativeGenerationAuditOut,
    NarrativeGenerationListItem,
    NarrativeGenerationOut,
    NarrativeGenerationStatusOut,
    NarrativeInsightAuditOut,
    NarrativeInsightOut,
    NarrativePayloadOut,
    NarrativeResultsResponse,
    NarrativeResultsSubmission,
)
from app.security.auth import require_internal_secret
from app.services.narrative_service import (
    NarrativeServiceError,
    get_project_narrative_generations,
    process_results,
)

router = APIRouter(prefix="/api/internal", dependencies=[Depends(require_internal_secret)])


def _get_generation_or_404(db: Session, generation_id: uuid.UUID) -> NarrativeGeneration:
    generation = db.get(NarrativeGeneration, generation_id)
    if generation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Narrative generation not found."
        )
    return generation


def _valid_insights(generation: NarrativeGeneration) -> list[NarrativeInsight]:
    return [i for i in generation.insights if i.validation_status == NarrativeValidationStatus.VALID]


def _build_generation_response(generation: NarrativeGeneration, insights, out_cls, insight_cls):
    return out_cls(
        id=generation.id,
        project_id=generation.project_id,
        narrative_types=generation.narrative_types,
        baseline_project_ids=generation.baseline_project_ids,
        comparison_project_ids=generation.comparison_project_ids,
        language=generation.language,
        status=generation.status,
        missing_narrative_types=generation.missing_narrative_types,
        model=generation.model,
        prompt_version=generation.prompt_version,
        prompt_contract_version=generation.prompt_contract_version,
        payload_schema_version=generation.payload_schema_version,
        validator_version=generation.validator_version,
        input_hash=generation.input_hash,
        regenerated_from_generation_id=generation.regenerated_from_generation_id,
        error_message=generation.error_message,
        created_at=generation.created_at,
        started_at=generation.started_at,
        completed_at=generation.completed_at,
        insights=[insight_cls.model_validate(i) for i in insights],
    )


@router.get(
    "/narrative-generations/{generation_id}/payload", response_model=NarrativePayloadOut
)
def get_narrative_payload(generation_id: uuid.UUID, db: Session = Depends(get_db)):
    """n8n-facing. Returns the persisted `source_snapshot` verbatim — never
    recomputed from current analytics.
    """
    generation = _get_generation_or_404(db, generation_id)
    return NarrativePayloadOut(
        generation_id=generation.id,
        narrative_types=generation.narrative_types,
        language=generation.language,
        prompt_contract_version=generation.prompt_contract_version,
        payload_schema_version=generation.payload_schema_version,
        source_snapshot=generation.source_snapshot,
    )


@router.post(
    "/narrative-generations/{generation_id}/results", response_model=NarrativeResultsResponse
)
def submit_narrative_results(
    generation_id: uuid.UUID,
    payload: NarrativeResultsSubmission,
    db: Session = Depends(get_db),
):
    """n8n-facing. Validates every candidate against the persisted
    snapshot, persists all of them (valid + rejected), and finalizes status.
    """
    generation = _get_generation_or_404(db, generation_id)
    try:
        generation = process_results(db, generation, payload)
    except NarrativeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return NarrativeResultsResponse(
        status=generation.status,
        missing_narrative_types=generation.missing_narrative_types or [],
    )


@router.get("/narrative-generations/{generation_id}", response_model=NarrativeGenerationOut)
def get_narrative_generation(generation_id: uuid.UUID, db: Session = Depends(get_db)):
    """Standard read path — valid insights only. Rejected candidates are
    never reachable here; use /audit for that.
    """
    generation = _get_generation_or_404(db, generation_id)
    return _build_generation_response(
        generation, _valid_insights(generation), NarrativeGenerationOut, NarrativeInsightOut
    )


@router.get(
    "/narrative-generations/{generation_id}/status",
    response_model=NarrativeGenerationStatusOut,
)
def get_narrative_generation_status(generation_id: uuid.UUID, db: Session = Depends(get_db)):
    generation = _get_generation_or_404(db, generation_id)
    return NarrativeGenerationStatusOut(
        id=generation.id,
        status=generation.status,
        missing_narrative_types=generation.missing_narrative_types,
    )


@router.get(
    "/narrative-generations/{generation_id}/audit",
    response_model=NarrativeGenerationAuditOut,
)
def get_narrative_generation_audit(generation_id: uuid.UUID, db: Session = Depends(get_db)):
    """Debugging only — valid and rejected candidates alike. Never used by
    the project Insights tab, the comparison view, or any downstream
    consumer.
    """
    generation = _get_generation_or_404(db, generation_id)
    return _build_generation_response(
        generation, generation.insights, NarrativeGenerationAuditOut, NarrativeInsightAuditOut
    )


@router.get(
    "/projects/{project_id}/narrative-generations",
    response_model=list[NarrativeGenerationListItem],
)
def list_project_narrative_generations(project_id: uuid.UUID, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    generations = get_project_narrative_generations(db, project_id)
    return [
        NarrativeGenerationListItem(
            id=g.id,
            status=g.status,
            narrative_types=g.narrative_types,
            scope="comparison" if g.baseline_project_ids else "project",
            created_at=g.created_at,
            model=g.model,
            prompt_version=g.prompt_version,
            input_hash=g.input_hash,
            regenerated_from_generation_id=g.regenerated_from_generation_id,
        )
        for g in generations
    ]
