FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN useradd --system --uid 1001 app

COPY pyproject.toml README.md ./
COPY src src
COPY migrations migrations
RUN pip install --no-cache-dir .

USER app
EXPOSE 8000
# Migrations run at startup under an advisory lock, so several workers/replicas are safe.
CMD ["tally", "serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
