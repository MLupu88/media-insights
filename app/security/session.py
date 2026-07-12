from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

_SALT = "msl-insights-session"


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(secret_key=settings.session_secret, salt=_SALT)


def create_session_value() -> str:
    return _serializer().dumps({"auth": True})


def is_session_value_valid(value: str | None, max_age: int | None = None) -> bool:
    if not value:
        return False
    settings = get_settings()
    effective_max_age = settings.session_max_age_seconds if max_age is None else max_age
    try:
        data = _serializer().loads(value, max_age=effective_max_age)
    except (BadSignature, SignatureExpired):
        return False
    return bool(isinstance(data, dict) and data.get("auth") is True)
