"""Finch is mocked with respx, so these run offline without keys."""

import os

import pytest
import respx
from httpx import Response

os.environ.setdefault("FINCH_CLIENT_ID", "test-id")
os.environ.setdefault("FINCH_CLIENT_SECRET", "test-secret")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.finch_client import FINCH_API_BASE  # noqa: E402
from app.formatting import display, money  # noqa: E402

IND = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def client():
    main._SESSIONS.clear()
    main._client = None
    with TestClient(main.app) as c:
        yield c


def _connect(client, mock):
    mock.post("/sandbox/connections").mock(
        return_value=Response(200, json={
            "connection_id": "conn_1", "provider_id": "gusto",
            "products": ["company", "directory", "individual", "employment"],
            "access_token": "SECRET-TOKEN", "token_type": "bearer",
        })
    )
    r = client.post("/connect", data={"provider_id": "gusto", "employee_size": 5}, follow_redirects=False)
    assert r.status_code == 303
    return r


# Token handling

@respx.mock(base_url=FINCH_API_BASE)
def test_token_never_reaches_the_browser(client, respx_mock):
    r = _connect(client, respx_mock)
    cookie = r.cookies.get("finch_session")
    assert cookie and "SECRET-TOKEN" not in cookie
    assert "SECRET-TOKEN" not in r.text
    # the server holds it, keyed by session id
    assert any(s["access_token"] == "SECRET-TOKEN" for s in main._SESSIONS.values())
    # and we only asked for the four allowed products
    body = respx_mock.calls.last.request.content
    assert b'"payment"' not in body and b'"pay_statement"' not in body


@respx.mock(base_url=FINCH_API_BASE)
def test_connect_falls_back_to_next_auth_method(client, respx_mock):
    """Justworks rejects api_token; the app should retry with credential and succeed."""
    route = respx_mock.post("/sandbox/connections")
    route.side_effect = [
        Response(400, json={"code": 400, "name": "validation_error",
                            "message": "Invalid provider and authentication type pairing: justworks + api_token"}),
        Response(200, json={"connection_id": "conn_2", "provider_id": "justworks",
                            "authentication_type": "credential", "products": ["company"],
                            "access_token": "T2", "token_type": "bearer"}),
    ]
    r = client.post("/connect", data={"provider_id": "justworks"}, follow_redirects=False)
    assert r.status_code == 303
    assert route.call_count == 2
    import json
    assert json.loads(route.calls[0].request.content)["authentication_type"] == "api_token"
    assert json.loads(route.calls[1].request.content)["authentication_type"] == "credential"
    assert any(s["connection"]["authentication_type"] == "credential" for s in main._SESSIONS.values())


# Null fields

def test_display_filter_turns_null_into_placeholder():
    assert "Not provided" in str(display(None))
    assert "Not provided" in str(display(""))
    assert "Not provided" in str(display([]))
    assert str(display("full_time")) == "Full time"
    assert str(display(True)) == "Yes"


def test_money_filter_handles_cents_and_null():
    assert str(money({"amount": 8500000, "currency": "usd", "unit": "yearly"})) == "USD 85,000.00 / yearly"
    assert "Not provided" in str(money(None))
    assert "Not provided" in str(money({"amount": None}))


@respx.mock(base_url=FINCH_API_BASE)
def test_null_fields_render_as_not_provided(client, respx_mock):
    _connect(client, respx_mock)
    respx_mock.get("/employer/company").mock(return_value=Response(200, json={
        "id": "c1", "legal_name": None, "entity": None, "ein": None,
        "primary_email": None, "primary_phone_number": None,
        "departments": None, "locations": [], "accounts": None,
    }))
    respx_mock.get("/employer/directory").mock(return_value=Response(200, json={
        "paging": {"count": 1, "offset": 0},
        "individuals": [{"id": IND, "first_name": "Ada", "middle_name": None, "last_name": None,
                         "manager": None, "department": None, "is_active": None}],
    }))
    r = client.get("/employer")
    assert r.status_code == 200
    assert "8 of 8 fields not provided" in r.text
    assert r.text.count("Not provided") >= 10
    assert "None" not in r.text.replace("None on file", "")  # no raw Python None leaks


# Provider doesn't implement an endpoint

@respx.mock(base_url=FINCH_API_BASE)
def test_501_on_company_shows_custom_banner_and_directory_still_loads(client, respx_mock):
    _connect(client, respx_mock)
    respx_mock.get("/employer/company").mock(return_value=Response(501, json={
        "code": 501, "name": "not_implemented_error",
        "message": "Not implemented", "finch_code": "not_implemented_error",
    }))
    respx_mock.get("/employer/directory").mock(return_value=Response(200, json={
        "paging": {"count": 1, "offset": 0},
        "individuals": [{"id": IND, "first_name": "Ada", "last_name": "Lovelace",
                         "manager": {"id": "m1"}, "department": {"name": "Eng"}, "is_active": True}],
    }))
    r = client.get("/employer")
    assert r.status_code == 200
    assert "Gusto does not implement /employer/company" in r.text
    assert "Ada" in r.text and "Lovelace" in r.text


@respx.mock(base_url=FINCH_API_BASE)
def test_batch_item_501_on_employment_shows_banner_but_individual_renders(client, respx_mock):
    _connect(client, respx_mock)
    respx_mock.post("/employer/individual").mock(return_value=Response(200, json={
        "responses": [{"individual_id": IND, "code": 200, "body": {
            "id": IND, "first_name": "Ada", "last_name": "Lovelace",
            "emails": [{"data": "ada@example.com", "type": "work"}],
            "phone_numbers": None, "dob": "1815-12-10", "residence": None,
        }}]
    }))
    respx_mock.post("/employer/employment").mock(return_value=Response(200, json={
        "responses": [{"individual_id": IND, "code": 501, "body": {
            "code": 501, "name": "not_implemented_error",
            "message": "Not implemented", "finch_code": "not_implemented_error",
        }}]
    }))
    r = client.get(f"/employee/{IND}")
    assert r.status_code == 200
    assert "ada@example.com" in r.text
    assert "Gusto does not implement /employer/employment" in r.text


# Payment endpoints are blocked

@pytest.mark.anyio
async def test_client_refuses_disallowed_paths():
    from app.finch_client import FinchClient, FinchError
    c = FinchClient("id", "secret")
    with pytest.raises(FinchError):
        await c._request("GET", "/employer/payment", "tok")
    with pytest.raises(FinchError):
        await c._request("POST", "/employer/pay-statement", "tok")
    await c.aclose()


# Sessions

def test_employee_without_session_is_401(client):
    r = client.get(f"/employee/{IND}")
    assert r.status_code == 401
    assert "Reconnect" in r.text


def test_unknown_provider_rejected(client):
    r = client.post("/connect", data={"provider_id": "not-a-provider"})
    assert r.status_code == 400
