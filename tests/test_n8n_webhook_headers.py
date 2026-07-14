import uuid
from unittest.mock import patch

import httpx

from app.services.n8n import (
    trigger_chat_run,
    trigger_classification,
    trigger_narrative_generation,
)


def _response(status_code: int = 202) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        request=httpx.Request("POST", "https://n8n.example.test/webhook"),
    )


@patch("app.services.n8n.httpx.post")
def test_narrative_trigger_uses_internal_secret_header_not_body(mock_post):
    mock_post.return_value = _response()
    generation_id = uuid.uuid4()
    project_id = uuid.uuid4()

    trigger_narrative_generation(generation_id, project_id)

    _, kwargs = mock_post.call_args
    assert kwargs["headers"] == {"x-internal-secret": "test-internal-secret"}
    assert kwargs["json"] == {
        "generation_id": str(generation_id),
        "project_id": str(project_id),
    }
    assert "secret" not in kwargs["json"]


@patch("app.services.n8n.httpx.post")
def test_chat_trigger_uses_internal_secret_header_not_body(mock_post):
    mock_post.return_value = _response()
    run_id = uuid.uuid4()
    session_id = uuid.uuid4()

    trigger_chat_run(run_id, session_id)

    _, kwargs = mock_post.call_args
    assert kwargs["headers"] == {"x-internal-secret": "test-internal-secret"}
    assert kwargs["json"] == {
        "run_id": str(run_id),
        "session_id": str(session_id),
    }
    assert "secret" not in kwargs["json"]


@patch("app.services.n8n.httpx.post")
def test_classification_trigger_contract_is_unchanged(mock_post):
    mock_post.return_value = _response()
    project_id = uuid.uuid4()

    trigger_classification(project_id)

    _, kwargs = mock_post.call_args
    assert kwargs["json"]["project_id"] == str(project_id)
    assert kwargs["json"]["secret"] == "test-internal-secret"
    assert "headers" not in kwargs
