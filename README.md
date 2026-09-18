# GridWise LLM Energy Optimizer

Django REST API for the GridWise preliminary challenge.

## Run locally

```bash
python -m venv venv
. venv/bin/activate
pip install -r requirements.txt
export DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost"
export DJANGO_DEBUG="true"
export DJANGO_SECURE_SSL_REDIRECT="false"
export GEMINI_API_KEY="your-key"
python manage.py runserver
```

Readiness check:

```bash
curl http://127.0.0.1:8000/health
```

The production process should run behind HTTPS with `gunicorn config.wsgi:application`.
Set `DJANGO_DEBUG=false`, `DJANGO_SECRET_KEY`, and `DJANGO_ALLOWED_HOSTS` in the
deployment environment. `GEMINI_API_KEY` is required for the LLM interpretation
stage; provider failures are logged and returned as safe no-op interpretations.

## API

- `GET /health` returns `{"status":"ok"}`.
- `POST /optimize-energy` accepts the exact request schema from the GridWise
  Problem Statement and returns the interpretation plus a 24-hour plan.

Run the test suite with:

```bash
python manage.py test
```
