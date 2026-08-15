FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 hansard && \
    useradd --uid 10001 --gid hansard --no-create-home --shell /usr/sbin/nologin hansard

COPY requirements.lock pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY config ./config
COPY alembic.ini ./alembic.ini

RUN python -m pip install --no-cache-dir -r requirements.lock && \
    mkdir -p /var/empty/hansard-processed && \
    chown -R hansard:hansard /app /var/empty/hansard-processed

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; host = os.environ['HANSARD_ALLOWED_HOSTS'].split(',')[0]; request = urllib.request.Request('http://127.0.0.1:8000/health/live', headers={'Host': host}); urllib.request.urlopen(request, timeout=3).read()"]

CMD ["python", "-m", "uvicorn", "hansard_annotator.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips=*"]
