"""Small HTTP client for the Finch API.

I'm using httpx directly instead of the Finch SDK so every request, header,
and error path is in one place.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx

FINCH_API_BASE = os.getenv("FINCH_API_BASE", "https://api.tryfinch.com")
FINCH_API_VERSION = "2020-09-17"

# The only products we ask for. The token Finch issues is scoped to these,
# so it can't call /employer/payment or /employer/pay-statement.
ALLOWED_PRODUCTS: tuple[str, ...] = ("company", "directory", "individual", "employment")

# Sandbox auth methods. We try them in order until the provider accepts one.
AUTH_METHODS: tuple[str, ...] = ("api_token", "credential", "oauth", "assisted")

# Second guard. The client only builds URLs for these paths.
ALLOWED_PATHS: frozenset[str] = frozenset(
    {"/employer/company", "/employer/directory", "/employer/individual", "/employer/employment"}
)

# Providers that work in the Finch Sandbox. GET /providers needs a bearer
# token, which we don't have until a connection exists, so this is a static list.
PROVIDERS: list[dict[str, str]] = [
    {"id": "gusto", "name": "Gusto"},
    {"id": "justworks", "name": "Justworks"},
    {"id": "bamboo_hr", "name": "BambooHR"},
    {"id": "workday", "name": "Workday"},
    {"id": "paychex_flex", "name": "Paychex Flex"},
    {"id": "adp_run", "name": "ADP RUN"},
    {"id": "bob", "name": "HiBob"},
    {"id": "rippling", "name": "Rippling"},
    {"id": "trinet", "name": "TriNet"},
    {"id": "zenefits", "name": "Zenefits"},
]


class FinchError(Exception):
    """Base error. `status` is the HTTP status Finch returned."""

    def __init__(self, message: str, status: int | None = None, finch_code: str | None = None):
        super().__init__(message)
        self.status = status
        self.finch_code = finch_code


class FinchNotImplemented(FinchError):
    """The provider doesn't implement this endpoint.

    Finch sends HTTP 501 with finch_code "not_implemented_error".
    """


class FinchUnauthorized(FinchError):
    """401 or 403. The token is missing, expired, or out of scope."""


def _raise_for_finch_error(resp: httpx.Response, endpoint: str) -> None:
    if resp.status_code < 400:
        return
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    message = payload.get("message") or resp.text or f"HTTP {resp.status_code}"
    finch_code = payload.get("finch_code")

    if resp.status_code == 501 or finch_code == "not_implemented_error":
        raise FinchNotImplemented(
            f"This provider does not implement {endpoint}.", status=501, finch_code=finch_code
        )
    if resp.status_code in (401, 403):
        raise FinchUnauthorized(message, status=resp.status_code, finch_code=finch_code)
    raise FinchError(message, status=resp.status_code, finch_code=finch_code)


class FinchClient:
    def __init__(self, client_id: str, client_secret: str, timeout: float = 20.0):
        if not client_id or not client_secret:
            raise ValueError("FINCH_CLIENT_ID and FINCH_CLIENT_SECRET must be set")
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._http = httpx.AsyncClient(base_url=FINCH_API_BASE, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # Sandbox connection -> access token

    async def create_sandbox_connection(self, provider_id: str, employee_size: int = 20) -> dict[str, Any]:
        """POST /sandbox/connections. Returns the response, including access_token.

        This is the only call that uses the client secret (Basic auth).
        Everything after it uses the bearer token.

        Providers support different auth methods. Gusto takes api_token,
        Justworks wants oauth, and so on. So we try each method in order and
        keep the first one Finch accepts.
        """
        rejections: list[str] = []
        for auth_type in AUTH_METHODS:
            resp = await self._http.post(
                "/sandbox/connections",
                headers={
                    "Authorization": f"Basic {self._basic}",
                    "Finch-API-Version": FINCH_API_VERSION,
                },
                json={
                    "provider_id": provider_id,
                    "authentication_type": auth_type,
                    "products": list(ALLOWED_PRODUCTS),
                    "employee_size": employee_size,
                },
            )
            try:
                _raise_for_finch_error(resp, "/sandbox/connections")
            except FinchError as e:
                # 400/422 means Finch didn't like this provider + auth pairing.
                # Try the next one. Anything else (bad creds, 5xx) isn't retryable.
                if e.status in (400, 422):
                    rejections.append(f"{auth_type}: {e}")
                    continue
                raise
            data = resp.json()
            data.setdefault("authentication_type", auth_type)
            return data
        raise FinchError(
            f"{provider_id} rejected every sandbox authentication method. " + " | ".join(rejections),
            status=400,
        )

    # Employer data (bearer token)

    async def _request(self, method: str, path: str, token: str, **kwargs: Any) -> Any:
        if path not in ALLOWED_PATHS:
            raise FinchError(f"Refusing to call disallowed endpoint {path}", status=403)
        resp = await self._http.request(
            method,
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Finch-API-Version": FINCH_API_VERSION,
            },
            **kwargs,
        )
        _raise_for_finch_error(resp, path)
        return resp.json()

    async def get_company(self, token: str) -> dict[str, Any]:
        return await self._request("GET", "/employer/company", token)

    async def get_directory(self, token: str, limit: int = 250, offset: int = 0) -> dict[str, Any]:
        return await self._request(
            "GET", "/employer/directory", token, params={"limit": limit, "offset": offset}
        )

    async def get_individual(self, token: str, individual_id: str) -> dict[str, Any]:
        """Batch endpoint. We send one id and unwrap the single result."""
        data = await self._request(
            "POST", "/employer/individual", token, json={"requests": [{"individual_id": individual_id}]}
        )
        return _unwrap_batch(data, "/employer/individual")

    async def get_employment(self, token: str, individual_id: str) -> dict[str, Any]:
        data = await self._request(
            "POST", "/employer/employment", token, json={"requests": [{"individual_id": individual_id}]}
        )
        return _unwrap_batch(data, "/employer/employment")


def _unwrap_batch(data: dict[str, Any], endpoint: str) -> dict[str, Any]:
    """Batch endpoints return HTTP 200 with a per-item `code` inside.

    Treat that inner code the same way as a top-level HTTP error.
    """
    responses = data.get("responses") or []
    if not responses:
        raise FinchError(f"Empty response from {endpoint}", status=502)
    item = responses[0]
    code = item.get("code", 200)
    body = item.get("body") or {}
    if code == 501 or body.get("finch_code") == "not_implemented_error":
        raise FinchNotImplemented(f"This provider does not implement {endpoint}.", status=501)
    if code >= 400:
        raise FinchError(body.get("message") or f"{endpoint} returned {code}", status=code)
    return body
