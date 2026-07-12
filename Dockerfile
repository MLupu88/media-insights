# --- Stage 1: build the Tailwind CSS bundle ---
FROM node:20-slim AS assets

WORKDIR /assets
COPY package.json ./
RUN npm install

COPY tailwind.config.js ./
COPY app/templates ./app/templates
COPY app/static/css ./app/static/css
COPY app/static/js ./app/static/js
RUN npm run build:css

# --- Stage 2: application image ---
FROM python:3.12-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

COPY --from=assets /assets/app/static/css/app.css ./app/static/css/app.css

RUN mkdir -p /app/data/uploads
VOLUME ["/app/data/uploads"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
