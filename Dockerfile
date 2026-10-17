FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

COPY common/ ./common/
COPY ingestion/ ./ingestion/
COPY api/ ./api/
COPY dashboard/ ./dashboard/

# Create a non-root user for security
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser
