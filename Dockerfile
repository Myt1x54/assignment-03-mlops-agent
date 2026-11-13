# Inference image for the churn prediction API (DECISION D24).
# Build after exporting the champion model:  uv run python scripts/export_model.py --alias champion
#                                             docker build -t mlops-api:0.1.0 --build-arg APP_VERSION=0.1.0 .
FROM python:3.12-slim

ARG APP_VERSION=0.1.0
LABEL org.opencontainers.image.title="churn-api" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION}

WORKDIR /app
COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY api/ /app/api/
COPY model/ /app/model/

RUN useradd --uid 10001 --create-home appuser && chown -R appuser /app
USER 10001

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
