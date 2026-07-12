from app.security.auth import require_internal_secret
from app.security.passwords import constant_time_equals
from app.security.session import create_session_value, is_session_value_valid
from fastapi import HTTPException
import pytest


def test_session_value_round_trips():
    value = create_session_value()
    assert is_session_value_valid(value) is True


def test_session_value_rejects_garbage():
    assert is_session_value_valid("not-a-real-session-token") is False


def test_session_value_rejects_missing_value():
    assert is_session_value_valid(None) is False
    assert is_session_value_valid("") is False


def test_session_value_expires_after_max_age():
    value = create_session_value()
    assert is_session_value_valid(value, max_age=-1) is False


def test_tampered_cookie_is_rejected_end_to_end(client):
    login_response = client.post(
        "/login", data={"password": "test-password"}, follow_redirects=False
    )
    assert login_response.status_code == 303

    client.cookies.set("msl_session", "tampered-value")
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_constant_time_equals():
    assert constant_time_equals("secret", "secret") is True
    assert constant_time_equals("secret", "other") is False


def test_require_internal_secret_accepts_matching_header():
    require_internal_secret(x_internal_secret="test-internal-secret")


def test_require_internal_secret_rejects_wrong_header():
    with pytest.raises(HTTPException) as exc_info:
        require_internal_secret(x_internal_secret="wrong-secret")
    assert exc_info.value.status_code == 401


def test_require_internal_secret_rejects_missing_header():
    with pytest.raises(HTTPException) as exc_info:
        require_internal_secret(x_internal_secret="")
    assert exc_info.value.status_code == 401
