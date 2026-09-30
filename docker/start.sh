#!/bin/sh
set -e

cd /app
uvicorn api:app --host 127.0.0.1 --port 8000 --workers 2 &
nginx -g 'daemon off;'
