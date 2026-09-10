FROM python:3.12-slim
WORKDIR /app

COPY agent2/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common

COPY agent2/ /app/agent2/
WORKDIR /app/agent2

EXPOSE 9002
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9002"]
