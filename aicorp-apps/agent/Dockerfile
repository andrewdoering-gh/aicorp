FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY agent.py .
COPY knowledge.py .
COPY governance.py .
COPY homelabops.html .
COPY product_manager.py .
COPY cto.py .
COPY workers.py .
COPY workstreams.py .
COPY execution.py .
COPY requirements.py .
COPY product.py .
COPY acceptance.py .
COPY notifications.py .

USER 65534:65534
ENTRYPOINT ["python", "/app/agent.py"]
