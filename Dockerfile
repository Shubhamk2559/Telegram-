# Koyeb worker image. Secrets image me nahi: sab env vars / Koyeb secrets se aate hain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=131072 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WORKSPACE_DIR=/app/workspace \
    PORT=8000

WORKDIR /app

# Non-root user
RUN useradd --create-home --uid 10001 app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src ./src
RUN mkdir -p /app/workspace && chown -R app:app /app
USER app

EXPOSE 8000

# Sirf local `docker run` ke liye (Koyeb apne health checks khud configure karta hai)
HEALTHCHECK --interval=60s --timeout=6s --start-period=90s --retries=3 \
  CMD ["python", "-m", "src.health_probe"]

# SIGTERM par graceful stop (worker state DB me safe rakhta hai). Migrations startup par apne aap lagti hain.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "src.app", "worker"]
