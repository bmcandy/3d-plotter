# Built by CI (publish.yml) with the repository root as build context and
# published to GHCR; the Supervisor pulls the image instead of building.
# BUILD_ARCH selects the HA base image (amd64, aarch64).
ARG BUILD_ARCH
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-debian:bookworm

# Debian base: every dependency has manylinux wheels for aarch64
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY backend/app/ ./app/
COPY --from=frontend /build/dist/ ./static/

COPY crealipen/run.sh /
RUN chmod a+x /run.sh
CMD ["/run.sh"]
