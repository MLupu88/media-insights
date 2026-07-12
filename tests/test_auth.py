from app.config import get_settings


def test_valid_login_sets_cookie_and_redirects(client):
    response = client.post(
        "/login", data={"password": "test-password"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    settings = get_settings()
    cookie = response.cookies.get(settings.session_cookie_name)
    assert cookie is not None


def test_valid_login_cookie_is_httponly_and_samesite_lax(client):
    response = client.post(
        "/login", data={"password": "test-password"}, follow_redirects=False
    )
    set_cookie_header = response.headers["set-cookie"]

    assert "HttpOnly" in set_cookie_header
    assert "samesite=lax" in set_cookie_header.lower()


def test_invalid_login_shows_error_and_no_cookie(client):
    response = client.post(
        "/login", data={"password": "wrong-password"}, follow_redirects=False
    )

    assert response.status_code == 401
    assert "Incorrect password" in response.text

    settings = get_settings()
    assert settings.session_cookie_name not in response.cookies


def test_unauthenticated_request_redirects_to_login(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_authenticated_root_returns_projects_page(authenticated_client):
    response = authenticated_client.get("/")

    assert response.status_code == 200
    assert "Media analysis projects" in response.text


def test_login_page_redirects_when_already_authenticated(authenticated_client):
    response = authenticated_client.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_logout_clears_session_and_redirects(authenticated_client):
    response = authenticated_client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    protected_response = authenticated_client.get("/", follow_redirects=False)
    assert protected_response.status_code == 307
    assert protected_response.headers["location"] == "/login"
