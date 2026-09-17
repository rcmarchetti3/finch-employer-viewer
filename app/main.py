"""Pick a Finch sandbox provider, browse the employer's data.

The access token lives in a server-side dict keyed by a random session id.
The browser only gets that session id, in a signed HttpOnly cookie.
"""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from .finch_client import (
    ALLOWED_PRODUCTS,
    PROVIDERS,
    FinchClient,
    FinchError,
    FinchNotImplemented,
    FinchUnauthorized,
)
from .formatting import register_filters

load_dotenv()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    if _client is not None:
        await _client.aclose()


app = FastAPI(title="Finch Employer Viewer", lifespan=lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
register_filters(templates.env)

# Sessions

SESSION_COOKIE = "finch_session"
_signer = URLSafeSerializer(os.getenv("SESSION_SECRET") or secrets.token_hex(32), salt="finch-session")

# session_id -> {"access_token", "provider_id", "connection", "names"}
# In memory on purpose. A restart drops every token. Use Redis for production.
_SESSIONS: dict[str, dict[str, Any]] = {}


def _get_session(request: Request) -> dict[str, Any] | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        session_id = _signer.loads(raw)
    except BadSignature:
        return None
    return _SESSIONS.get(session_id)


def _set_session(response: RedirectResponse, data: dict[str, Any]) -> None:
    session_id = secrets.token_urlsafe(32)
    _SESSIONS[session_id] = data
    response.set_cookie(
        SESSION_COOKIE,
        _signer.dumps(session_id),
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    )


# Finch client

_client: FinchClient | None = None


def get_client() -> FinchClient:
    global _client
    if _client is None:
        _client = FinchClient(os.getenv("FINCH_CLIENT_ID", ""), os.getenv("FINCH_CLIENT_SECRET", ""))
    return _client


# Helpers

def _full_name(p: dict[str, Any]) -> str | None:
    name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
    return name or None


def _provider_name(provider_id: str) -> str:
    return next((p["name"] for p in PROVIDERS if p["id"] == provider_id), provider_id)


async def _safe(coro, endpoint: str, provider_id: str) -> tuple[Any, dict[str, str] | None]:
    """Run a Finch call and turn any error into a banner dict.

    Returns (data, error). One of them is always None. Each section of the
    page goes through this on its own, so a 501 on employment doesn't take
    the directory down with it.
    """
    try:
        return await coro, None
    except FinchNotImplemented:
        return None, {
            "kind": "not_implemented",
            "title": f"{_provider_name(provider_id)} does not implement {endpoint}",
            "detail": "Finch returned 501 not_implemented_error for this provider. "
                      "The data is not available through this integration.",
        }
    except FinchUnauthorized as e:
        return None, {
            "kind": "unauthorized",
            "title": "Access token rejected",
            "detail": f"Finch returned {e.status}. The token may have expired or lack scope for {endpoint}. "
                      "Reconnect to get a new one.",
        }
    except FinchError as e:
        return None, {
            "kind": "error",
            "title": f"Could not load {endpoint}",
            "detail": str(e),
        }


# Routes

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    session = _get_session(request)
    if session:
        return RedirectResponse("/employer", status_code=303)
    return templates.TemplateResponse(
        request, "index.html", {"providers": PROVIDERS, "products": ALLOWED_PRODUCTS, "error": None}
    )


@app.post("/connect")
async def connect(
    request: Request,
    provider_id: str = Form(...),
    employee_size: int = Form(20),
    client: FinchClient = Depends(get_client),
):
    if provider_id not in {p["id"] for p in PROVIDERS}:
        return templates.TemplateResponse(
            request, "index.html",
            {"providers": PROVIDERS, "products": ALLOWED_PRODUCTS, "error": "Unknown provider."},
            status_code=400,
        )
    try:
        conn = await client.create_sandbox_connection(provider_id, employee_size=max(1, min(employee_size, 250)))
    except FinchError as e:
        return templates.TemplateResponse(
            request, "index.html",
            {"providers": PROVIDERS, "products": ALLOWED_PRODUCTS,
             "error": f"Finch could not create a sandbox connection: {e}"},
            status_code=502,
        )

    token = conn.pop("access_token")  # keep it out of anything we render or log
    response = RedirectResponse("/employer", status_code=303)
    _set_session(response, {"access_token": token, "provider_id": provider_id, "connection": conn})
    return response


@app.post("/disconnect")
async def disconnect(request: Request):
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        try:
            _SESSIONS.pop(_signer.loads(raw), None)
        except BadSignature:
            pass
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/employer", response_class=HTMLResponse)
async def employer(request: Request, client: FinchClient = Depends(get_client)):
    session = _get_session(request)
    if not session:
        return RedirectResponse("/", status_code=303)
    token, provider_id = session["access_token"], session["provider_id"]

    company, company_err = await _safe(client.get_company(token), "/employer/company", provider_id)
    directory, directory_err = await _safe(client.get_directory(token), "/employer/directory", provider_id)

    # Finch only gives us the manager's id. Map ids to names from the
    # directory so the page shows a name instead of a UUID.
    individuals = (directory or {}).get("individuals", []) or []
    names = {p["id"]: _full_name(p) for p in individuals if p.get("id")}
    session["names"] = names

    return templates.TemplateResponse(
        request, "employer.html",
        {
            "names": names,
            "provider_name": _provider_name(provider_id),
            "provider_id": provider_id,
            "connection": session["connection"],
            "company": company, "company_err": company_err,
            "directory": (directory or {}).get("individuals", []), "directory_err": directory_err,
            "paging": (directory or {}).get("paging"),
        },
    )


@app.get("/employee/{individual_id}", response_class=HTMLResponse)
async def employee(request: Request, individual_id: str, client: FinchClient = Depends(get_client)):
    """HTML fragment with individual + employment for one employee."""
    session = _get_session(request)
    if not session:
        return HTMLResponse('<div class="banner error">Session expired. <a href="/">Reconnect</a>.</div>', status_code=401)
    token, provider_id = session["access_token"], session["provider_id"]

    individual, individual_err = await _safe(client.get_individual(token, individual_id), "/employer/individual", provider_id)
    employment, employment_err = await _safe(client.get_employment(token, individual_id), "/employer/employment", provider_id)

    return templates.TemplateResponse(
        request, "partials/employee.html",
        {
            "names": session.get("names", {}),
            "individual": individual, "individual_err": individual_err,
            "employment": employment, "employment_err": employment_err,
        },
    )
