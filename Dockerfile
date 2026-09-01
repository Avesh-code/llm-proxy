FROM python:3.12-slim

# Unbuffered stdout so `docker logs -f` shows request lines as they happen.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copied first, on its own, so a change to proxy.py does not invalidate the
# pip layer — rebuilds after a code edit take a second instead of a minute.
COPY requirement.txt .
RUN pip install --no-cache-dir -r requirement.txt

COPY proxy.py admin.html backends.yaml ./

# Drop root — the proxy needs no write access to anything except data/,
# where the admin UI persists backends/teams/tracing config (data/config.json).
# NB: Debian already ships a system account called "proxy" (uid 13), so the
# name here must be something else or useradd fails with "already exists".
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data && chown appuser:appuser /app/data
USER appuser

EXPOSE 4000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://localhost:' + os.getenv('PROXY_PORT','4000') + '/health')"

CMD ["python", "proxy.py"]
