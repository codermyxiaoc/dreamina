FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/dreamina \
    LOGIN_BROWSER_CHANNEL=chromium \
    LOGIN_BROWSER_HEADLESS=true

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 dreamina \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin dreamina

# Explicit source allowlist: no local credentials, captures, outputs or profiles.
COPY dreamina_api/ ./dreamina_api/
COPY web/ ./web/
COPY deploy/api-entrypoint.sh /app/api-entrypoint.sh
RUN mkdir -p /app/outputs /home/dreamina/.cache \
    && chmod 755 /app/api-entrypoint.sh \
    && chmod -R a+rX /ms-playwright \
    && chown -R dreamina:dreamina /app/outputs /home/dreamina

USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)"
ENTRYPOINT ["/app/api-entrypoint.sh"]
CMD ["python", "-m", "dreamina_api", "serve", "--host", "0.0.0.0", "--port", "8787"]
