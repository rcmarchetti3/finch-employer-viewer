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
