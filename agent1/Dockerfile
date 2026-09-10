FROM python:3.12-slim
WORKDIR /app

COPY agent1/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ /app/common/
RUN pip install --no-cache-dir /app/common

COPY agent1/ /app/agent1/
WORKDIR /app/agent1

EXPOSE 9001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9001"]
