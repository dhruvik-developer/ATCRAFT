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
# --installed is the mode for a deployment host: the runbook copies the
# templates to /etc/nginx and substitutes the real hostnames there, so the
# repository copies keep example.com and the default mode refuses them (as it
# must - and editing them in place would dirty the tree `deploy.sh up`
# requires clean). It reads the installed files, and the placeholder check is
# aimed at them, which is what the handover checklist asks for.
#
# Worth running before every `systemctl reload nginx`, and especially after
# changing the domains: a bad host config takes BOTH deployments offline, and
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
             "$INSTALLED_CONF_D/atcraft-dev.conf" "$INSTALLED_SNIPPETS/atcraft-proxy.conf"; do
      [ -r "$f" ] || { echo "error: $f is missing or unreadable - install the host configs first (ONE-BOX-RUNBOOK.md, host nginx step)" >&2; exit 1; }
    done
    if grep -q 'example\.com' "$INSTALLED_CONF_D"/atcraft-*.conf; then
      echo "error: the installed host nginx still contains example.com placeholders" >&2
      echo "run the sed lines from the runbook against $INSTALLED_CONF_D/atcraft-*.conf, then run this check again" >&2
      exit 1
    fi
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

mkdir -p /etc/nginx/snippets /var/www/certbot /var/log/nginx
if [ "${MODE:-}" = "installed" ]; then
  # The box's own files, bind-mounted read-only by the outer script.
  echo "validating the installed host configuration (/etc/nginx/conf.d, /etc/nginx/snippets)"
  cp /host/snippets/*.conf /etc/nginx/snippets/
  cp /host/conf.d/00-default.conf   /etc/nginx/conf.d/
  cp /host/conf.d/atcraft-main.conf /etc/nginx/conf.d/
  cp /host/conf.d/atcraft-dev.conf  /etc/nginx/conf.d/
else
  echo "validating the repository copies (infrastructure/nginx/host)"
  cp /repo/infrastructure/nginx/host/snippets/*.conf /etc/nginx/snippets/
  cp /repo/infrastructure/nginx/host/00-default.conf   /etc/nginx/conf.d/
  cp /repo/infrastructure/nginx/host/atcraft-main.conf /etc/nginx/conf.d/
  cp /repo/infrastructure/nginx/host/atcraft-dev.conf  /etc/nginx/conf.d/
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
if [ "$ports" -eq 2 ]; then
  echo "OK: the two deployments have distinct upstream ports"
else
  echo "FAIL: expected 2 distinct upstream ports, found $ports"
  echo "      Both hostnames would serve the same deployment - and it would work."
  exit 1
fi

names=$(grep -h -E "^[[:space:]]*server_name" /etc/nginx/conf.d/atcraft-*.conf | awk '{print $2}' | tr -d ';' | sort -u | wc -l)
if [ "$names" -ge 2 ]; then
  echo "OK: the two deployments have distinct hostnames"
else
  echo "FAIL: the two configs share a server_name - nginx would use whichever loaded first"
  exit 1
fi

echo
echo "--- operational endpoints closed on the public hostnames ---"
rc=0
for f in /etc/nginx/conf.d/atcraft-main.conf /etc/nginx/conf.d/atcraft-dev.conf; do
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
MSYS_NO_PATHCONV=1 docker run --rm "${mounts[@]}" -e MODE="$MODE" \
  --entrypoint sh nginx:1.30.4-alpine3.24 "/repo/${RUNNER}"
