# Base image is a build argument so the runtime can be pinned (by tag or by
# digest) without editing this file.
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

# Identity of the runtime account. Group 0 by default: hosts that assign an
# arbitrary uid at run time (OpenShift and friends) keep the process in the
# root group, so a group-writable /data stays writable for whatever uid lands.
ARG APP_UID=10001
ARG APP_GID=0
ARG APP_USER_NAME=appuser
# Defaults for the runtime settings that also shape the image layout.
ARG DATA_DIR=/data
ARG APP_PORT=8080

# Only defaults — every one of these is overridable at run time, and a platform
# that injects its own PORT wins over what is baked in here. DB_PATH is
# deliberately absent: the app derives it from DATA_DIR unless it is set.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=${DATA_DIR} \
    PORT=${APP_PORT} \
    RUN_UID=${APP_UID} \
    RUN_GID=${APP_GID}

WORKDIR /app
COPY app.py docker-entrypoint.py /app/
RUN set -eux; \
    if ! getent passwd "${APP_UID}" >/dev/null; then \
        useradd --system --uid "${APP_UID}" --gid "${APP_GID}" \
                --no-create-home --home-dir /nonexistent "${APP_USER_NAME}"; \
    fi; \
    mkdir -p "${DATA_DIR}"; \
    chown -R "${APP_UID}:${APP_GID}" /app "${DATA_DIR}"; \
    chmod -R g+rwX "${DATA_DIR}"

USER ${APP_UID}:${APP_GID}
# Metadata only; EXPOSE cannot read a runtime $PORT, the healthcheck below can.
EXPOSE ${APP_PORT}
# No VOLUME directive: docker-compose.yml supplies the named volume, and
# declaring it here would strand an anonymous volume on every plain
# `docker run` that forgets -v.
#
# The timing flags cannot be parameterised (Dockerfile does not expand
# variables in HEALTHCHECK options), so compose overrides them from the
# environment; these values are the standalone `docker run` defaults.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/healthz', timeout=2)" || exit 1

# The entrypoint prepares DATA_DIR and, when the platform starts the container
# as root to fix volume ownership, drops back to RUN_UID:RUN_GID before exec'ing
# this command. exec means the app itself is PID 1 and receives SIGTERM.
ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
CMD ["python", "/app/app.py"]
