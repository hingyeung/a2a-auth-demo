FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY mockmcp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mockmcp/ /app/

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 9003
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9003/.well-known/oauth-protected-resource', timeout=3)" || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9003"]
