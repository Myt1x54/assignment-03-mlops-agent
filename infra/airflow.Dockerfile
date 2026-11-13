# Airflow 3 image with an isolated virtualenv for the ML training code (DECISION D9).
ARG AIRFLOW_IMAGE=apache/airflow:3.1.0-python3.12
FROM ${AIRFLOW_IMAGE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*' \
 && mkdir -p /opt/ml-venv && chown airflow: /opt/ml-venv

USER airflow
COPY infra/ml-requirements.txt /tmp/ml-requirements.txt
RUN python -m venv /opt/ml-venv \
 && /opt/ml-venv/bin/pip install --no-cache-dir -r /tmp/ml-requirements.txt
