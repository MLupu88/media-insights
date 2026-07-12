def test_health_endpoint(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "msl-insights"


def test_health_does_not_require_authentication(client):
    response = client.get("/api/health")
    assert response.status_code == 200


def test_health_details_endpoint(client):
    response = client.get("/api/health/details")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database_connected"] is True
    assert body["upload_directory_writable"] is True
    assert "version" in body
    assert "environment" in body
    assert "timestamp" in body


def test_health_details_does_not_expose_secrets(client):
    response = client.get("/api/health/details")
    body_text = response.text.lower()

    assert "test-internal-secret" not in body_text
    assert "test-session-secret" not in body_text
    assert "test-password" not in body_text
