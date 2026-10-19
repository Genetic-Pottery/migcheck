# Plane's API test dependencies, from the Plane commit passed as the "plane"
# build context. See README.md for the build command.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libpq-dev libxmlsec1-dev pkg-config git && rm -rf /var/lib/apt/lists/*
COPY --from=plane requirements /req
RUN pip install --no-cache-dir -r /req/test.txt
