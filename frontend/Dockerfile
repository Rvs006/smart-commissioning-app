# Production frontend image: Vite build served by nginx.
# Build context must be the repository root (see infra/docker-compose.yml):
#   docker build -f frontend/Dockerfile .

# --- Stage 1: build the static bundle ---------------------------------------
# Node 22 ships npm 11, matching the npm that generated package-lock.json;
# node:20 (npm 10) rejects the lockfile's optional-dependency tree under npm ci.
FROM node:22-alpine AS build

WORKDIR /build

# Install dependencies first so they cache independently of source changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# --- Stage 2: serve with nginx ----------------------------------------------
FROM nginx:stable-alpine

# SPA fallback + /api reverse proxy to the api service (see infra/).
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html

EXPOSE 80
