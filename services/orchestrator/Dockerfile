# Build context is the repository root so the shared outbox package can be
# installed alongside the service.
FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser

COPY packages/favl-outbox /tmp/favl-outbox
RUN pip install --no-cache-dir /tmp/favl-outbox && rm -rf /tmp/favl-outbox

COPY services/orchestrator/pyproject.toml .
RUN pip install --no-cache-dir .

COPY services/orchestrator/app ./app
COPY services/orchestrator/migrations ./migrations
COPY services/orchestrator/alembic.ini services/orchestrator/entrypoint.sh ./
RUN chmod +x entrypoint.sh
USER appuser
EXPOSE 8000
CMD ["./entrypoint.sh"]
