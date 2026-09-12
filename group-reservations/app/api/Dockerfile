FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src

RUN pip install --no-cache-dir . \
    && mkdir -p /app/.local \
    && chown -R pwuser:pwuser /app

USER pwuser
EXPOSE 8000

CMD ["uvicorn", "groupreservations.api:app", "--host", "0.0.0.0", "--port", "8000"]
