# =============================================================================
# Atcraft ERP - React frontend image (master rules section 48)
#   - multi-stage: node builds, nginx serves
#   - production stage ships static assets only, no Node runtime
#   - development stage runs the Vite dev server with hot reload
# =============================================================================

# ------------------------------------------------------------------- deps ---
FROM node:22.12-alpine AS deps

WORKDIR /app

# Cached on the lockfile alone so a source edit does not reinstall everything.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# ----------------------------------------------------------- development ----
FROM node:22.12-alpine AS development

WORKDIR /app
ENV NODE_ENV=development

COPY --from=deps /app/node_modules ./node_modules
COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ------------------------------------------------------------------ build ---
FROM node:22.12-alpine AS build

WORKDIR /app
ENV NODE_ENV=production

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# The API base path is baked into the bundle at build time. It is a relative
# path so the same image works behind any hostname - nginx routes /api to the
# backend container.
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN npm run build

# ------------------------------------------------------------- production ---
FROM nginx:1.27-alpine AS production

# Serve as an unprivileged user. nginx:alpine already defines the `nginx` user;
# it needs write access to its runtime directories to start without root.
RUN mkdir -p /var/cache/nginx /var/run \
 && chown -R nginx:nginx /var/cache/nginx /var/run /usr/share/nginx/html

COPY --from=build --chown=nginx:nginx /app/dist /usr/share/nginx/html
COPY --chown=nginx:nginx nginx.conf /etc/nginx/conf.d/default.conf

USER nginx
EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget --spider -q http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
