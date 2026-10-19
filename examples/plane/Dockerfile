FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libxmlsec1-dev pkg-config git && rm -rf /var/lib/apt/lists/*
COPY requirements /req
RUN pip install --no-cache-dir -r /req/test.txt
