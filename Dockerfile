FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/budget.db \
    PORT=8080

WORKDIR /app
COPY app.py /app/app.py
RUN useradd --system --uid 10001 --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser
# Metadata only, and it cannot read $PORT — the healthcheck below is the part
# that has to follow an overridden port.
EXPOSE 8080
# No VOLUME directive: docker-compose.yml supplies the named volume, and
# declaring it here would strand an anonymous volume on every plain
# `docker run` that forgets -v.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/healthz', timeout=2)" || exit 1
CMD ["python", "/app/app.py"]
