# GridWise — Energy Optimization API

A Django REST API that converts natural-language operator notes into a
validated, cost-optimal 24-hour battery/solar/grid dispatch schedule.

## How it works

```
Energy Data + Operator Notes
        │
        ▼
  LLM Interpreter        (llm.py)      — Gemini turns notes into raw directives
        │
        ▼
  Guardrail Validator     (guardrails.py) — sanitizes/clamps untrusted LLM output
        │
        ▼
  Math Optimizer          (optimizer.py)  — PuLP/CBC linear program
        │
        ▼
  Final Validator         (optimizer.py)  — re-checks energy balance & SoC bounds
        │
        ▼
     API Response
```

- **LLM Interpreter (`llm.py`)** — Sends the battery parameters and up to 3
  operator notes to Gemini (`gemini-2.5-flash` by default) and asks it to
  classify each note into one of six directive types (`solar_reduction`,
  `minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`,
  `max_grid_window`, `no_op`). Output is treated as **untrusted**. Includes an
  in-process cache and a retry with safe `no_op` fallback if Gemini is
  unavailable, misconfigured, or returns malformed JSON.

- **Guardrail Validator (`guardrails.py`)** — Deterministically re-validates
  every LLM directive: unknown types, missing fields, out-of-range hours, or
  bad numbers are all coerced to a safe `no_op` rather than raising. Guarantees
  exactly one interpretation per note.

- **Math Optimizer (`optimizer.py`)** — Builds a linear program over 24 hourly
  variables (grid draw, solar use, charge, discharge, state of charge) that
  minimizes total grid cost subject to the validated directives, then
  reconstructs a rounded, internally-consistent hourly plan and re-validates
  energy balance, solar limits, battery transitions, and end-of-day
  neutrality before returning it.

- **API layer (`views.py`, `serializers.py`)** — Validates the request shape,
  orchestrates the pipeline, and returns controlled error responses
  (`422` for infeasible scenarios, `500` for anything else) without leaking
  stack traces.

## API

### `GET /health`
Returns `{"status": "ok"}`.

### `POST /optimize-energy`
**Request body:**
```json
{
  "scenario_id": "string",
  "operator_notes": ["1 to 3 natural-language strings"],
  "hours": [
    {
      "hour": 0,
      "demand_kwh": 10.0,
      "solar_kwh": 0.0,
      "tariff_bdt_per_kwh": 5.0
    }
    // ... exactly 24 entries, one per hour 0-23
  ],
  "battery": {
    "capacity_kwh": 100.0,
    "initial_energy_kwh": 50.0,
    "minimum_energy_kwh": 10.0,
    "max_charge_kwh_per_hour": 25.0,
    "max_discharge_kwh_per_hour": 25.0
  }
}
```

**Response (200):**
```json
{
  "scenario_id": "string",
  "directive_interpretation": [ /* one entry per operator note */ ],
  "hourly_plan": [ /* 24 entries: grid_kwh, solar_used_kwh, battery_action, ... */ ],
  "total_grid_kwh": 0.0,
  "total_cost_bdt": 0.0,
  "peak_grid_kwh": 0.0,
  "plan_summary": "string"
}
```

**Error responses:**
- `400` — request failed schema validation (bad field types, wrong hour count, `initial_energy_kwh` > `capacity_kwh`, etc.)
- `422` — request was valid but no feasible schedule exists for the scenario
- `500` — unexpected internal error

## Setup

### Requirements
- Python 3.12
- A Gemini API key (optional — the service degrades to `no_op` for every note if unset)

### Local development
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # then fill in real values
python manage.py migrate
python manage.py runserver
```

### Environment variables (`.env`)
| Variable | Default | Notes |
|---|---|---|
| `DJANGO_SECRET_KEY` | dev fallback | **Set a real secret in production.** |
| `DJANGO_DEBUG` | `false` | Set `true` only for local dev. |
| `DJANGO_ALLOWED_HOSTS` | `*` | Comma-separated; restrict in production. |
| `DJANGO_EMAIL_BACKEND` | SMTP backend | Override if you don't need email. |
| `DJANGO_SECURE_SSL_REDIRECT` | `false` | Set `true` behind HTTPS in production. |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` | Set in production once HTTPS is confirmed working. |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | `false` | |
| `DJANGO_SECURE_HSTS_PRELOAD` | `false` | |
| `GEMINI_API_KEY` | *(none)* | **Required** for real LLM interpretation; without it every note defaults to `no_op`. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | |
| `LLM_TIMEOUT_MS` | `12000` | Per-request timeout to Gemini. |

> ⚠️ Don't commit `.env`. It's already excluded via `.gitignore` /
> `.dockerignore`, but if a real key has ever been shared or committed,
> rotate it.

### Tests
```bash
python manage.py test
```
Covers serializer validation, guardrail edge cases (unsupported directive
types, out-of-range values), the LP solver (including end-of-day battery
neutrality), and end-to-end API behavior with the LLM call mocked.

### Docker
```bash
docker build -t gridwise .
docker run --env-file .env -p 8000:8000 gridwise
```
The image installs dependencies, runs `collectstatic`, and serves via
Gunicorn (`config.wsgi:application`) on port 8000 with 2 workers.

## Project structure
```
config/            # Django project (settings, urls, wsgi, asgi)
core/
  views.py         # optimize-energy / health endpoints
  serializers.py   # request/response schemas
  llm.py           # Gemini-based note → directive interpreter
  guardrails.py    # deterministic validation of LLM output
  optimizer.py     # PuLP linear program + plan reconstruction/validation
  tests.py
requirements.txt
Dockerfile
```

## Notes / things to watch
- `ALLOWED_HOSTS` defaults to `*` — fine for local/Docker testing, but should
  be locked down before any real deployment.
- `SECRET_KEY` falls back to an insecure dev value if the env var isn't set —
  make sure production always sets `DJANGO_SECRET_KEY`.
- The optimizer assumes `battery.initial_energy_kwh == battery` end-of-day
  target (SoC neutrality is enforced as a hard constraint); scenarios that
  can't satisfy this alongside the operator directives will return `422`.