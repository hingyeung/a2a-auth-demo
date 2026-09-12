FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY agent2/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common

COPY agent2/ /app/agent2/
WORKDIR /app/agent2

RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /data && chown -R appuser:appuser /data
USER appuser

EXPOSE 9002
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9002/health', timeout=3)" || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9002"]
