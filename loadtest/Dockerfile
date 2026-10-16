FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
RUN pip install --no-cache-dir locust==2.32.4 httpx==0.27.0
COPY loadtest ./loadtest
