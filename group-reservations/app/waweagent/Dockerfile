FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY app/waweagent/main.py /app/main.py

RUN pip install --no-cache-dir . \
    && python -m playwright install chromium \
    && chown -R pwuser:pwuser /app

USER pwuser
EXPOSE 8080
CMD ["python", "/app/main.py"]
