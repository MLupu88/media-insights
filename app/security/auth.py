from fastapi import Header, HTTPException, Request, status

from app.config import get_settings
from app.security.passwords import constant_time_equals
from app.security.session import is_session_value_valid


class NotAuthenticated(Exception):
    """Raised by page routes when no valid session cookie is present."""


def is_request_authenticated(request: Request) -> bool:
    settings = get_settings()
    cookie_value = request.cookies.get(settings.session_cookie_name)
    return is_session_value_valid(cookie_value)


def require_web_session(request: Request) -> None:
    if not is_request_authenticated(request):
        raise NotAuthenticated()


def require_internal_secret(
    x_internal_secret: str = Header(default=""),
) -> None:
    settings = get_settings()
    if not constant_time_equals(x_internal_secret, settings.media_app_internal_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal secret.",
        )
