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

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "ingestion.runner"]
