# syntax=docker/dockerfile:1
# Build from the repo root so the shared M-Pesa port and fake come along:
#   docker build -f services/payments/Dockerfile -t local/payments:pr .
FROM python:3.12-slim

ARG COMMIT_SHA=local
ENV COMMIT_SHA=${COMMIT_SHA}

# Patch OS packages so Trivy HIGH/CRITICAL on gzip/pcre2/sqlite/perl stay clear.
RUN apt-get update \
  && apt-get upgrade -y --no-install-recommends \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY services/_shared/mpesa ./mpesa
COPY services/payments/app.py services/payments/reconcile.py ./
COPY services/payments/core ./core
RUN mkdir -p /app/data && chown 10001:10001 /app/data

USER 10001:10001
EXPOSE 8080

ENV PORT=8080
ENV DATABASE_URL=sqlite:////app/data/payments.db
CMD ["python", "-u", "app.py"]
