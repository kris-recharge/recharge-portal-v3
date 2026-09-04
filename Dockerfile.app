FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY app/ ./app/

# Operator entry points. These live at the repo root and are NOT imported by the
# app, but without them in the image `docker exec rca_api_v3 python run_payter.py`
# — the documented way to force a collector cycle instead of waiting out the
# scheduler's 15-minute timer — fails with "can't open file" (hit 2026-08-20).
COPY run_payter.py run_nayax_portal.py ./

# NOTE: do NOT COPY .env into the image. Configuration (including DEV_BYPASS_AUTH
# and APP_ENV) is supplied at runtime via docker-compose `env_file`. Baking a
# developer .env into the image previously shipped DEV_BYPASS_AUTH=true to
# production, which disabled Supabase auth and per-user EVSE filtering for every
# user. A .dockerignore also excludes .env from the build context as a backstop.

EXPOSE 8000

# Single worker ON PURPOSE: the lifespan starts in-process background services
# (alert poll thread, APScheduler Payter collector, SSE broadcast queues).
# With >1 worker each process runs its own copy -> duplicate alert emails,
# doubled DB polling, connector-count double-counting, and SSE clients
# connected to a worker that never fires alerts.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
