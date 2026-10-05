FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common

COPY orchestrator/ /app/orchestrator/
WORKDIR /app/orchestrator

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 9001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9001/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9001"]
