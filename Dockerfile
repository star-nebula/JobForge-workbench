FROM python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    JOBFORGE_HOST=0.0.0.0 \
    JOBFORGE_PORT=8080 \
    JOBFORGE_LOG_FORMAT=json

WORKDIR /app

COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

COPY src ./src
COPY web ./web

RUN useradd --create-home --shell /usr/sbin/nologin jobforge \
    && mkdir -p /app/data \
    && chown -R jobforge:jobforge /app

USER jobforge
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3).read()"]

CMD ["python", "-m", "jobforge.server"]
