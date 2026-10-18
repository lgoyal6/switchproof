FROM node:22-bookworm-slim AS dashboard
WORKDIR /build/web
COPY web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
COPY switchproof ./switchproof
COPY vendor ./vendor
RUN uv sync --locked --no-dev
COPY --from=dashboard /build/web/dist ./web/dist
COPY scripts/cloud-start.sh ./scripts/cloud-start.sh
EXPOSE 8080
CMD ["sh", "/app/scripts/cloud-start.sh"]
