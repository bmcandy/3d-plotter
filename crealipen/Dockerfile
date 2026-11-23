# BUILD_ARCH is provided by the Supervisor (aarch64, amd64, ...)
ARG BUILD_ARCH
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-debian:bookworm

# Debian base: every dependency has manylinux wheels for aarch64
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY static/ ./static/

COPY run.sh /
RUN chmod a+x /run.sh
CMD ["/run.sh"]
