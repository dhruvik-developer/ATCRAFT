# =============================================================================
# Atcraft ERP - React frontend image (master rules section 48)
#   - multi-stage: node builds, nginx serves
#   - production stage ships static assets only, no Node runtime
#   - development stage runs the Vite dev server with hot reload
# =============================================================================

# ------------------------------------------------------------------- deps ---
FROM node:22.23.2-alpine3.24@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS deps

WORKDIR /app

# Cached on the lockfile alone so a source edit does not reinstall everything.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# ----------------------------------------------------------- development ----
FROM node:22.23.2-alpine3.24@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS development

WORKDIR /app
ENV NODE_ENV=development

COPY --from=deps /app/node_modules ./node_modules
COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ------------------------------------------------------------------ build ---
FROM node:22.23.2-alpine3.24@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS build

WORKDIR /app
ENV NODE_ENV=production

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Vite inlines VITE_* at build time, so everything the bundle needs has to be
# an ARG here. An arg passed by compose but not declared in this file is
# silently discarded - the build succeeds and the value simply is not there,
# which is how a release stamp ends up permanently reading "dev".
#
# Nothing secret may be added to this list. Whatever is named here is written
# into dist/ verbatim and served to every visitor; check-bundle-secrets.mjs
# runs as part of `npm run build` below and fails this stage if it is.

# Relative, so one image works behind any hostname - the deployment's nginx
# routes /api/v1 to its own backend.
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

# Which deployment and which build produced a browser crash report.
ARG VITE_APP_RELEASE=local
ENV VITE_APP_RELEASE=$VITE_APP_RELEASE

# Where crash reports are posted. Empty disables reporting, which is the
# default; the payload carries no tokens, bodies, query strings or emails.
ARG VITE_ERROR_REPORT_URL=
ENV VITE_ERROR_REPORT_URL=$VITE_ERROR_REPORT_URL

RUN npm run build

# ------------------------------------------------------------- production ---
FROM nginx:1.30.4-alpine3.24@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c AS production

# Serve as an unprivileged user. nginx:alpine already defines the `nginx` user;
# it needs write access to its runtime directories to start without root.
#
# The pid file is created and chowned as a FILE, deliberately. The obvious
# spelling - `chown -R nginx:nginx /var/run` - does nothing useful: /var/run is
# a symlink to /run, and chown with -R does not traverse symlinks, so it
# retargets the link itself and leaves /run owned by root. nginx then starts,
# logs its version and worker method, and dies on the last step before serving:
#
#   [emerg] open() "/run/nginx.pid" failed (13: Permission denied)
#
# which reads like a configuration error rather than a permission one. Creating
# the file first and giving nginx that one file is narrower than opening up /run
# and does not depend on which of the two paths the main config names.
RUN mkdir -p /var/cache/nginx \
 && touch /var/run/nginx.pid \
 && chown nginx:nginx /var/run/nginx.pid \
 && chown -R nginx:nginx /var/cache/nginx /usr/share/nginx/html

COPY --from=build --chown=nginx:nginx /app/dist /usr/share/nginx/html
COPY --chown=nginx:nginx nginx.conf /etc/nginx/conf.d/default.conf

USER nginx
EXPOSE 80

# 127.0.0.1, not localhost. In this image `localhost` resolves to ::1 first,
# and nginx is listening on IPv4 only: the image's own
# 10-listen-on-ipv6-by-default.sh entrypoint script adds `listen [::]:80` only
# when conf.d/default.conf is the packaged one, and this image replaces it. So
# the probe was refused while the container was serving every request
# correctly - the container reported unhealthy, `up --wait` failed, and nothing
# was actually wrong with it.
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD wget --spider -q http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
