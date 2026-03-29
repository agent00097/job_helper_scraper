#!/bin/bash
set -e

# Optional DATABASE_URL for local dev when using a single connection string.
# In Kubernetes, prefer DB_* env vars (see db.py) so passwords are not URL-encoded in the shell.
if [ -z "$DATABASE_URL" ] && [ -n "$DB_USER" ] && [ -n "$DB_PASSWORD" ] && [ -n "$DB_HOST" ] && [ -n "$DB_PORT" ] && [ -n "$DB_NAME" ]; then
    SSLMODE="${PGSSLMODE:-disable}"
    export DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}?sslmode=${SSLMODE}"
fi

exec "$@"
