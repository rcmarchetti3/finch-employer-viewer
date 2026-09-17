# Finch Employer Viewer

A small FastAPI app that creates a [Finch Sandbox](https://developer.tryfinch.com/implementation-guide/Test/Finch-Sandbox) connection for a provider you pick, exchanges it for an access token, and displays the employer's **company**, **directory**, **individual**, and **employment** data field by field. Click any employee in the directory to load their individual and employment details.

## Run it locally

Requires Python 3.11+ and a Finch sandbox application (sign up at [dashboard.tryfinch.com](https://dashboard.tryfinch.com/signup) and copy the client ID and secret).

```bash
git clone <this repo> && cd finch-employer-viewer
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and fill in FINCH_CLIENT_ID and FINCH_CLIENT_SECRET

uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000, choose a provider, click **Create connection**.

Run the tests (they mock Finch, no network or keys needed):

```bash
pip install -r requirements-dev.txt
pytest
```

## How it works

```
browser ──form POST /connect──▶ FastAPI ──POST /sandbox/connections (Basic client_id:secret)──▶ Finch
        ◀── 303 + signed session cookie ──┘   stores access_token server-side, keyed by session id

browser ──GET /employer────────▶ FastAPI ──GET /employer/company, /employer/directory (Bearer)──▶ Finch
browser ──GET /employee/{id}───▶ FastAPI ──POST /employer/individual, /employer/employment──────▶ Finch
        ◀── server-rendered HTML partial ──┘
```

| File | What it does |
|---|---|
| `app/finch_client.py` | All Finch HTTP calls. Maps Finch errors to typed exceptions (`FinchNotImplemented`, `FinchUnauthorized`, `FinchError`). Refuses to build a URL outside the four allowed endpoints. |
| `app/main.py` | Routes, session handling, and `_safe()`, which turns any Finch error into a banner for one section of the page without failing the others. |
| `app/formatting.py` | Jinja filters. `display` is the single place that decides how a null field looks. `money` converts Finch's cents to dollars. |
| `app/templates/` | Plain HTML + a few lines of vanilla JS to swap in the employee panel. No frontend build. |
| `tests/test_app.py` | One test per behaviour: token handling, nulls, 501s, blocked endpoints. |

## Design notes

**Where the access token lives.** In a server-side dictionary keyed by a random session id. The browser gets that session id in a signed, `HttpOnly`, `SameSite=Lax` cookie and nothing else. The token is popped off the Finch response before anything is rendered or logged. Restarting the server drops every token, which is fine for sandbox credentials; a real deployment would move `_SESSIONS` to Redis or a database and encrypt the token at rest.

**Why the token can't call `/payment` or `/pay-statement`.** Two independent guards. First, the sandbox connection is created with `products: ["company", "directory", "individual", "employment"]`, so Finch itself scopes the token and rejects payment calls. Second, `FinchClient._request` checks the path against an allowlist before sending, so there is no code path in this app that could reach those endpoints even if the scope were wider.

**Null fields.** Every value on every page goes through the `display` filter, which renders `None`, Jinja `Undefined` (from a null parent object), empty strings, and empty lists as an italic *Not provided*. Nested objects use `(obj or {}).field` so a null `department` or `entity` degrades the same way. Each section shows a count of how many fields the provider left empty, so nulls are visible instead of silently blank. Money is formatted from cents with its pay unit.

**Provider doesn't implement an endpoint.** Finch returns HTTP `501` with `finch_code: not_implemented_error`. For the batch endpoints (`/individual`, `/employment`) the HTTP status is 200 and the 501 is inside `responses[n].code`, so both shapes are handled and raised as `FinchNotImplemented`. The route wraps each call in `_safe()`, which renders a specific banner ("Gusto does not implement /employer/employment") in that section only. The rest of the page still loads.

**Provider list.** Curated in `finch_client.PROVIDERS`. Finch's `GET /providers` needs a bearer token, which doesn't exist until a connection does, so a static list is the simplest correct choice for the sandbox.

## Given more time

- Pull the provider list from `GET /providers` after the first connection and cache it, so new providers appear without a code change, and show each provider's supported products up front.
- Paginate the directory (`limit`/`offset`) and batch `/individual` and `/employment` requests instead of one id per click.
- Persist sessions in Redis with an expiry, encrypt tokens at rest, and add a "disconnect" call to Finch so tokens are revoked, not just forgotten.
- Handle the real Finch Connect OAuth flow (`/connect/authorize` and `/auth/token`) in addition to the sandbox shortcut, and support `Provider Sandboxes` for testing against real provider demo accounts.
- Listen for Finch webhooks (`account.updated`, `job.completed`) and refresh the view when a sync finishes rather than re-fetching on every page load.
- Structured logging with request ids, and rate-limit awareness (sandbox allows 10 refresh syncs per hour).
