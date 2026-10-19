#!/usr/bin/env bash
# Runs a command for Plane's API inside the dependency image, from the current checkout.
exec docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD":/code -w /code/apps/api \
  -e DATABASE_URL -e SECRET_KEY=migcheck -e REDIS_URL=redis://127.0.0.1:6390/0 -e AMQP_URL=memory:// \
  migcheck-plane-env "$@"
