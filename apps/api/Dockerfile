FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# --- development image: full dev deps, source mounted as a volume ---
FROM base AS development
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY . .
RUN chmod +x docker-entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- production image: runtime deps only, no source volume ---
FROM base AS production
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .
RUN chmod +x docker-entrypoint.sh && useradd --create-home --uid 1000 appuser
USER appuser
EXPOSE 8000
# docker-entrypoint.sh validates Settings() once, synchronously, before
# handing off to uvicorn — --workers' multiprocess supervisor otherwise
# treats a worker crashing on import (e.g. the production fail-fast
# validator rejecting an unsafe config) as transient and respawns it
# forever instead of ever exiting the container.
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
