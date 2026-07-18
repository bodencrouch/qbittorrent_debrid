FROM node:20-bookworm-slim AS frontend
WORKDIR /ui
COPY qbx/web/matcher/package.json qbx/web/matcher/package-lock.json ./
RUN npm ci
COPY qbx/web/matcher/ ./
RUN npm run build

FROM python:3.12-slim

# qbx runtime env: config in a volume, downloads in a mapped dir.
ENV QBX_CONFIG_DIR=/config \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY qbx ./qbx
COPY --from=frontend /ui/dist ./qbx/web/matcher/dist
RUN pip install .

RUN mkdir -p /config /downloads
VOLUME ["/config", "/downloads"]

EXPOSE 8484

# Bind to all interfaces inside the container; the compose file maps the port.
CMD ["qbx", "serve", "--host", "0.0.0.0", "--port", "8484"]
