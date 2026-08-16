# Production frontend image: Vite build served by nginx.
# Build context must be the repository root (see infra/docker-compose.yml):
#   docker build -f frontend/Dockerfile .

# --- Stage 1: build the static bundle ---------------------------------------
# Node 24 meets React Router 8's maintained runtime baseline and ships npm 11,
# matching the npm that generated package-lock.json.
FROM node:24-alpine AS build

ARG APP_VERSION=v0.1.50
ENV VITE_APP_VERSION="${APP_VERSION}"

WORKDIR /build

# Install dependencies first so they cache independently of source changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# --- Stage 2: serve with nginx ----------------------------------------------
FROM nginx:stable-alpine

ARG APP_VERSION=v0.1.50
ARG SOURCE_COMMIT=unknown
ARG SOURCE_REPOSITORY=https://github.com/Rvs006/smart-commissioning-app

LABEL org.opencontainers.image.title="Smart Commissioning Frontend" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      org.opencontainers.image.source="${SOURCE_REPOSITORY}"

# SPA fallback + /api reverse proxy to the api service (see infra/).
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80
