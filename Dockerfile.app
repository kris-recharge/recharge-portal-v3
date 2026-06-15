FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY app/ ./app/

# NOTE: do NOT COPY .env into the image. Configuration (including DEV_BYPASS_AUTH
# and APP_ENV) is supplied at runtime via docker-compose `env_file`. Baking a
# developer .env into the image previously shipped DEV_BYPASS_AUTH=true to
# production, which disabled Supabase auth and per-user EVSE filtering for every
# user. A .dockerignore also excludes .env from the build context as a backstop.

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
