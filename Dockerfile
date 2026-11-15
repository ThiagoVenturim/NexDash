# Multi-architecture Dockerfile for NexDash semantic query and dashboard server
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HOST=0.0.0.0

WORKDIR /app

# Create a non-privileged system user for runtime execution
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Copy build definition and sources
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY examples/ ./examples/

# Install nexdash package and dependencies
RUN pip install --no-cache-dir .

# Ownership and security
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

ENTRYPOINT ["python", "-m", "nexdash"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
