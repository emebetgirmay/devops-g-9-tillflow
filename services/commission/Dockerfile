# syntax=docker/dockerfile:1
# Scheduled jobs, not a server: EventBridge runs one-off tasks that override the command
# (close.py or disburse.py). Standard library only, so nothing to pip install.
#   docker build -t local/commission:pr services/commission
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
COPY close.py disburse.py worker.py ./
COPY ledger ./ledger
RUN mkdir -p /app/data && chown 10001:10001 /app/data

USER 10001:10001

ENV DATABASE_URL=sqlite:////app/data/commission.db
ENTRYPOINT ["python", "-u"]
CMD ["disburse.py"]
