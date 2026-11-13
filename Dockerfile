FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivileged user with a fixed UID so the host data folder can be owned by it.
RUN groupadd --system --gid 10001 sgapp \
 && useradd --system --uid 10001 --gid sgapp --no-create-home --shell /usr/sbin/nologin sgapp

WORKDIR /app
COPY app/requirements.txt .
# pip is not needed at runtime; removing it afterwards shrinks the attack surface
RUN pip install --no-cache-dir -r requirements.txt \
 && (pip uninstall -y setuptools wheel >/dev/null 2>&1 || true) \
 && pip uninstall -y pip

COPY --chown=root:root app/ /app/
RUN chmod -R a-w /app \
 && mkdir -p /data /run/sock && chown sgapp:sgapp /data /run/sock && chmod 0770 /run/sock

USER 10001:10001
# The container has no network at all; it is reached only through a Unix
# socket shared with the nginx proxy container.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s --start-interval=2s CMD ["python", "/app/healthcheck.py"]

# One worker process (keeps the login rate-limiter in one place) with threads.
# umask 007 => socket is rw for the sgapp group only (nginx is added to it).
CMD ["gunicorn", "--bind", "unix:/run/sock/app.sock", "--umask", "007", \
     "--workers", "1", "--threads", "8", "--timeout", "30", \
     "--limit-request-line", "4094", "--access-logfile", "-", \
     "--worker-tmp-dir", "/tmp", "app:app"]
