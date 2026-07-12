import uuid

import httpx

from app.config import get_settings


class N8nTriggerError(Exception):
    """Raised when the n8n classification webhook cannot be reached or fails."""


def trigger_classification(project_id: uuid.UUID) -> None:
    settings = get_settings()
    payload = {
        "secret": settings.media_app_internal_secret,
        "project_id": str(project_id),
    }

    try:
        response = httpx.post(
            settings.n8n_classification_webhook_url,
            json=payload,
            timeout=settings.n8n_request_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise N8nTriggerError("The classification service timed out.") from exc
    except httpx.RequestError as exc:
        raise N8nTriggerError(f"Could not reach the classification service: {exc}") from exc

    if not (200 <= response.status_code < 300):
        raise N8nTriggerError(
            f"The classification service returned an unexpected status "
            f"({response.status_code})."
        )


def trigger_narrative_generation(generation_id: uuid.UUID, project_id: uuid.UUID) -> None:
    settings = get_settings()
    payload = {
        "secret": settings.media_app_internal_secret,
        "generation_id": str(generation_id),
        "project_id": str(project_id),
    }

    try:
        response = httpx.post(
            settings.n8n_narrative_webhook_url,
            json=payload,
            timeout=settings.n8n_request_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise N8nTriggerError("The narrative generation service timed out.") from exc
    except httpx.RequestError as exc:
        raise N8nTriggerError(
            f"Could not reach the narrative generation service: {exc}"
        ) from exc

    if not (200 <= response.status_code < 300):
        raise N8nTriggerError(
            f"The narrative generation service returned an unexpected status "
            f"({response.status_code})."
        )
