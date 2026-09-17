# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG COMMIT_SHA=local
ENV COMMIT_SHA=${COMMIT_SHA}

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY app.py .

USER 10001:10001
EXPOSE 8080

ENV PORT=8080
CMD ["python", "-u", "app.py"]
