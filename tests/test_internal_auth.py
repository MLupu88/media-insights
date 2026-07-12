"""Internal-secret authentication, exercised against a representative
internal endpoint (project summary) that requires no request body.
"""

import uuid


def _summary_url(project_id) -> str:
    return f"/api/internal/projects/{project_id}/summary"


def test_internal_endpoint_rejects_missing_secret(client):
    response = client.get(_summary_url(uuid.uuid4()))
    assert response.status_code == 401


def test_internal_endpoint_rejects_invalid_secret(client):
    response = client.get(
        _summary_url(uuid.uuid4()), headers={"x-internal-secret": "wrong-secret"}
    )
    assert response.status_code == 401


def test_internal_endpoint_accepts_valid_secret(client, internal_headers, project_factory):
    project = project_factory()
    response = client.get(_summary_url(project.id), headers=internal_headers)
    assert response.status_code == 200


def test_internal_endpoint_missing_secret_does_not_leak_project_existence(
    client, project_factory
):
    project = project_factory()
    response = client.get(_summary_url(project.id))
    assert response.status_code == 401
