from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.security.auth import is_request_authenticated
from app.security.passwords import constant_time_equals
from app.security.session import create_session_value

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login")
def login_form(request: Request):
    if is_request_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "authenticated": False}
    )


@router.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    settings = get_settings()

    if not constant_time_equals(password, settings.app_password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect password. Please try again.", "authenticated": False},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=create_session_value(),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request):
    settings = get_settings()
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        samesite="lax",
        secure=settings.is_production,
    )
    return response
