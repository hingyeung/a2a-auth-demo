FROM python:3.12-slim
WORKDIR /app
COPY mockmcp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mockmcp/ /app/
EXPOSE 9003
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9003"]
