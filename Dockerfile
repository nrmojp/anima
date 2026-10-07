# syntax=docker/dockerfile:1.7
FROM python:3.14-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends espeak-ng \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 anima
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir '.[bot]'

COPY config ./config
RUN mkdir -p /app/state && chown -R anima:anima /app/config /app/state

USER anima
ENV ANIMA_ROOT=/app/config \
    ANIMA_STATE_ROOT=/app/state \
    PYTHONUNBUFFERED=1

VOLUME ["/app/config", "/app/state"]
EXPOSE 8765
CMD ["anima-bot"]
