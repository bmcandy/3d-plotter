#!/usr/bin/env bash
# Sync the deployment copy under crealipen/ from the sources.
#
# The HA Supervisor builds the app image with crealipen/ as its build
# context, so the compiled frontend and a copy of the backend must be
# committed there. Run this after changing backend/app or frontend/src
# and commit the result.
set -euo pipefail
cd "$(dirname "$0")/.."

(cd frontend && npm ci && npm run build)

rm -rf crealipen/static
cp -r frontend/dist crealipen/static

rm -rf crealipen/app
cp -r backend/app crealipen/app
find crealipen/app -type d -name '__pycache__' -exec rm -rf {} +
