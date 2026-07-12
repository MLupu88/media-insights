from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import analytics, auth, classification, comparison, files, internal, pages
from app.api import health as health_router
from app.api import narrative_generations, narratives
from app.config import get_settings
from app.database import get_db
from app.security.auth import NotAuthenticated

settings = get_settings()

app = FastAPI(title=settings.app_name)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.exception_handler(NotAuthenticated)
def handle_not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(url="/login")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/db")
def health_db(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ok"})


app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(files.router)
app.include_router(classification.router)
app.include_router(health_router.router)
app.include_router(internal.router)
app.include_router(analytics.router)
app.include_router(comparison.router)
app.include_router(narratives.router)
app.include_router(narrative_generations.router)
