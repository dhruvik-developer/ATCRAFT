#!/usr/bin/env bash
# =============================================================================
# Validate the HOST nginx configuration before reloading it.
#
#   ./scripts/check-host-nginx.sh              # the repository copies, customised
#   ./scripts/check-host-nginx.sh --template   # the uncustomised templates (CI)
#   ./scripts/check-host-nginx.sh --installed  # /etc/nginx/conf.d + snippets, on the box
#
# Loads the host configuration inside a real nginx, with self-signed
# certificates generated at the exact paths the configs name. Without those,
# `nginx -t` fails because a certificate is missing, which says nothing about
# whether the configuration is right.
#
# It loads it twice: in the deployment's own nginx runtime, and in the oldest
# nginx a host may run (MIN_HOST_NGINX). The configs use directives older
# releases refuse - `http2 on;` arrived in 1.25.1 - and Ubuntu's own package is
# 1.18 on 22.04 and 1.24 on 24.04, which is why the runbook installs nginx from
# the nginx.org repository. Both images are nginx.org builds, carrying the same
# nginx.conf that repository's package installs on the host.
#
# --installed is the mode for a deployment host: the runbook copies the
# templates to /etc/nginx and substitutes the real hostnames there, so the
# repository copies keep example.com and the default mode refuses them (as it
# must - and editing them in place would dirty the tree `deploy.sh up`
# requires clean). It reads the installed files, and the placeholder check is
# aimed at them, which is what the handover checklist asks for. It also reads
# the box's own `nginx -v` and refuses a version below MIN_HOST_NGINX: a config
# that loads in a container proves nothing about a host nginx that cannot parse
# it. The box's own `sudo nginx -t`, which the runbook runs next, has the last
# word.
#
# Worth running before every `systemctl reload nginx`, and especially after
# changing the domains: a bad host config takes production offline, and
# the failure is at reload time on the live box.
#
# This exists because writing these files was not enough. Two duplicate-directive
# faults - proxy_buffering, then proxy_read_timeout - were in the first version,
# each of which makes nginx refuse to load the entire configuration, and neither
# is visible by reading. The deployment configs had the identical fault and had
# never been loaded by anything either.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The deployment's nginx runtime (docker-compose.yml), then the oldest nginx a
# host may run. Keep the second image's version equal to MIN_HOST_NGINX.
MIN_HOST_NGINX="1.25.1"
NGINX_IMAGES=(
  nginx:1.30.6-alpine3.24-slim@sha256:dede8895648b461178c3bc604f7eed4423efe5220b2c4d839f5eb3d34ac3f8f7
  nginx:1.25.1-alpine-slim@sha256:7d7e3ec705ac1d04fbdf4537ad009d5f170a035a494e96f6eb0f828c10c062b6
)

# True when dotted version $1 is at least $2, compared numerically part by part
# (1.100.0 is newer than 1.25.1).
version_at_least() {
  local IFS=. i
  local -a have need
  read -r -a have <<<"$1"
  read -r -a need <<<"$2"
  for i in 0 1 2; do
    if (( 10#${have[i]:-0} > 10#${need[i]:-0} )); then return 0; fi
    if (( 10#${have[i]:-0} < 10#${need[i]:-0} )); then return 1; fi
  done
  return 0
}

# The box's own nginx is what loads the installed files on reload. /usr/sbin is
# not on every user's PATH, so it is searched as well.
require_host_nginx_version() {
  local nginx_bin reported version
  nginx_bin=$(PATH="$PATH:/usr/sbin:/usr/local/sbin" command -v nginx || true)
  if [ -z "$nginx_bin" ]; then
    echo "error: this host has no nginx - install nginx $MIN_HOST_NGINX or later from the nginx.org repository (ONE-BOX-RUNBOOK.md, step 8)" >&2
    return 1
  fi
  reported=$("$nginx_bin" -v 2>&1 || true)
  version=$(printf '%s\n' "$reported" | sed -n 's#^nginx version: [^/]*/\([0-9][0-9.]*\).*#\1#p' | head -n 1)
  if [ -z "$version" ]; then
    echo "error: could not read the host nginx version from '$nginx_bin -v': $reported" >&2
    return 1
  fi
  if ! version_at_least "$version" "$MIN_HOST_NGINX"; then
    echo "error: the host runs nginx $version, and the host configuration needs nginx $MIN_HOST_NGINX or later (it uses 'http2 on;')" >&2
    echo "install nginx from the nginx.org repository (ONE-BOX-RUNBOOK.md, step 8), then run this check again" >&2
    return 1
  fi
  echo "host nginx $version (at least $MIN_HOST_NGINX)"
}

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }

MODE="repo"
case "${1:-}" in
  "") ;;
  --template)  MODE="template" ;;
  --installed) MODE="installed" ;;
  *) echo "usage: $0 [--template|--installed]" >&2; exit 2 ;;
esac

# The committed files are templates. A deployment check must fail while the
# files it validates still name the documentation domain; CI passes --template
# because it is deliberately validating the uncustomised templates.
INSTALLED_CONF_D=/etc/nginx/conf.d
INSTALLED_SNIPPETS=/etc/nginx/snippets
case "$MODE" in
  template) ;;
  installed)
    for f in "$INSTALLED_CONF_D/00-default.conf" "$INSTALLED_CONF_D/atcraft-main.conf" \
             "$INSTALLED_SNIPPETS/atcraft-proxy.conf"; do
      [ -r "$f" ] || { echo "error: $f is missing or unreadable - install the host configs first (ONE-BOX-RUNBOOK.md, host nginx step)" >&2; exit 1; }
    done
    if grep -q 'example\.com' "$INSTALLED_CONF_D"/atcraft-*.conf; then
      echo "error: the installed host nginx still contains example.com placeholders" >&2
      echo "run the sed lines from the runbook against $INSTALLED_CONF_D/atcraft-*.conf, then run this check again" >&2
      exit 1
    fi
    require_host_nginx_version || exit 1
    ;;
  repo)
    if grep -q 'example\.com' infrastructure/nginx/host/atcraft-*.conf; then
      echo "error: host nginx still contains example.com placeholders" >&2
      echo "replace them with the real hostnames, then run this check again" >&2
      echo "(on a deployment host, validate the installed copies with: $0 --installed)" >&2
      exit 1
    fi
    ;;
esac

RUNNER=".check-host-nginx.$$.sh"
cleanup() { rm -f "$REPO_ROOT/$RUNNER"; }
trap cleanup EXIT

cat > "$REPO_ROOT/$RUNNER" <<'INNER'
#!/bin/sh
set -e
apk add --no-cache openssl >/dev/null 2>&1
echo "=== $(nginx -v 2>&1) ==="

mkdir -p /etc/nginx/snippets /var/www/certbot /var/log/nginx
if [ "${MODE:-}" = "installed" ]; then
  # The box's own files, bind-mounted read-only by the outer script.
  echo "validating the installed host configuration (/etc/nginx/conf.d, /etc/nginx/snippets)"
  cp /host/snippets/*.conf /etc/nginx/snippets/
  cp /host/conf.d/00-default.conf   /etc/nginx/conf.d/
  cp /host/conf.d/atcraft-main.conf /etc/nginx/conf.d/
else
  echo "validating the repository copies (infrastructure/nginx/host)"
  cp /repo/infrastructure/nginx/host/snippets/*.conf /etc/nginx/snippets/
  cp /repo/infrastructure/nginx/host/00-default.conf   /etc/nginx/conf.d/
  cp /repo/infrastructure/nginx/host/atcraft-main.conf /etc/nginx/conf.d/
fi
rm -f /etc/nginx/conf.d/default.conf

# Certificates at whatever paths the configs actually name, so this keeps
# working when the domains are changed.
for cert in $(grep -h -oE "ssl_certificate[[:space:]]+[^;]+" /etc/nginx/conf.d/atcraft-*.conf | awk '{print $2}' | sort -u); do
    dir=$(dirname "$cert")
    mkdir -p "$dir"
    [ -f "$dir/fullchain.pem" ] && continue
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
      -keyout "$dir/privkey.pem" -out "$dir/fullchain.pem" \
      -subj "/CN=$(basename "$dir")" >/dev/null 2>&1
done

echo "--- nginx -t ---"
# ssl_stapling warns on a self-signed certificate because there is no issuer to
# fetch. Expected here, absent with a real certificate.
nginx -t 2>&1 | grep -v "ssl_stapling" || true
nginx -t >/dev/null 2>&1 || { echo "FAILED: see above"; exit 1; }
echo "configuration loads"

grep -qE 'listen 80 default_server;' /etc/nginx/conf.d/00-default.conf \
  && grep -qE 'listen 443 ssl default_server;' /etc/nginx/conf.d/00-default.conf \
  && grep -qE 'ssl_reject_handshake on;' /etc/nginx/conf.d/00-default.conf \
  || { echo "FAILED: unknown hosts are not rejected by a default server"; exit 1; }
echo "default host/SNI is rejected"

echo
echo "--- routing ---"
grep -h -E "^[[:space:]]*(server_name|proxy_pass http://127)" /etc/nginx/conf.d/atcraft-*.conf \
  | sed 's/^[[:space:]]*//' | sort -u

echo
ports=$(grep -h -oE "127\.0\.0\.1:[0-9]+" /etc/nginx/conf.d/atcraft-*.conf | sort -u | wc -l)
if [ "$ports" -eq 1 ]; then
  echo "OK: production proxies to exactly one upstream port"
else
  echo "FAIL: expected 1 upstream port, found $ports"
  exit 1
fi

echo
echo "--- operational endpoints closed on the public hostnames ---"
rc=0
for f in /etc/nginx/conf.d/atcraft-main.conf; do
  for path in "/health/" "/metrics"; do
    if grep -q "location .*${path}.*return 404" "$f"; then
      echo "OK: $(basename "$f") closes $path"
    else
      echo "FAIL: $(basename "$f") does not close $path"
      rc=1
    fi
  done
done
exit $rc
INNER

mounts=(-v "${REPO_ROOT}:/repo:ro")
if [ "$MODE" = "installed" ]; then
  # Read-only, and no sudo needed: the daemon reads the bind mount as root.
  mounts+=(-v "${INSTALLED_CONF_D}:/host/conf.d:ro" -v "${INSTALLED_SNIPPETS}:/host/snippets:ro")
fi
for image in "${NGINX_IMAGES[@]}"; do
  MSYS_NO_PATHCONV=1 docker run --rm "${mounts[@]}" -e MODE="$MODE" \
    --entrypoint sh "$image" "/repo/${RUNNER}"
  echo
done
