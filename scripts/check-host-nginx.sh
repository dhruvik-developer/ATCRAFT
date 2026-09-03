#!/usr/bin/env bash
# =============================================================================
# Validate the HOST nginx configuration before installing it.
#
#   ./scripts/check-host-nginx.sh
#
# Loads infrastructure/nginx/host/ inside a real nginx, with self-signed
# certificates generated at the exact paths the configs name. Without those,
# `nginx -t` fails because a certificate is missing, which says nothing about
# whether the configuration is right.
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

RUNNER=".check-host-nginx.$$.sh"
cleanup() { rm -f "$REPO_ROOT/$RUNNER"; }
trap cleanup EXIT

cat > "$REPO_ROOT/$RUNNER" <<'INNER'
#!/bin/sh
set -e
apk add --no-cache openssl >/dev/null 2>&1

mkdir -p /etc/nginx/snippets /var/www/certbot /var/log/nginx
cp /repo/infrastructure/nginx/host/snippets/*.conf /etc/nginx/snippets/
cp /repo/infrastructure/nginx/host/atcraft-main.conf /etc/nginx/conf.d/
cp /repo/infrastructure/nginx/host/atcraft-dev.conf  /etc/nginx/conf.d/
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

MSYS_NO_PATHCONV=1 docker run --rm \
  -v "${REPO_ROOT}:/repo:ro" \
  --entrypoint sh nginx:1.27-alpine "/repo/${RUNNER}"
