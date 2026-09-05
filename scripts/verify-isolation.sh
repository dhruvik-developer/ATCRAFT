#!/usr/bin/env bash
# =============================================================================
# Prove the two deployments cannot reach each other.
#
#   ./scripts/verify-isolation.sh
#
# Both deployments must already be up (./scripts/deploy.sh <name> up). This
# script does not start or stop anything and does not write to either database
# beyond one marker row per side, which it removes.
#
# The point is not to re-read the compose files - they say what they say. It is
# to attempt each crossing from inside deployment 1 and watch it fail, because
# a configuration that looks isolated and a configuration that IS isolated are
# different claims and only one of them can be tested.
#
# Every check states what a PASS means, so a green run is readable as evidence
# rather than as a row of ticks.
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_DIM=$'\033[2m'
else
  C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_DIM=""
fi

PASSED=0
FAILED=0
SKIPPED=0

section() { printf '\n%s\n' "${C_BOLD}$*${C_RESET}"; }
pass()    { PASSED=$((PASSED+1)); printf '  %sPASS%s %s\n' "$C_GREEN" "$C_RESET" "$1"; }
fail()    { FAILED=$((FAILED+1)); printf '  %sFAIL%s %s\n' "$C_RED" "$C_RESET" "$1"; }
skip()    { SKIPPED=$((SKIPPED+1)); printf '  %sSKIP%s %s\n' "$C_DIM" "$C_RESET" "$1"; }
note()    { printf '       %s%s%s\n' "$C_DIM" "$1" "$C_RESET"; }

env_value() { sed -n "s/^$2=//p" "deploy/$1.env" | tail -n 1 | tr -d '\r'; }

for d in main dev; do
  [ -f "deploy/$d.env" ] || { printf '%serror: deploy/%s.env is missing - run ./scripts/deploy.sh %s init%s\n' "$C_RED" "$d" "$d" "$C_RESET" >&2; exit 1; }
done

MAIN_PROJECT="$(env_value main COMPOSE_PROJECT_NAME)"
DEV_PROJECT="$(env_value dev COMPOSE_PROJECT_NAME)"
MAIN_PORT="$(env_value main HTTP_PORT)"
DEV_PORT="$(env_value dev HTTP_PORT)"
MAIN_DB="$(env_value main POSTGRES_DB)"; : "${MAIN_DB:=atcraft_erp}"
DEV_DB="$(env_value dev POSTGRES_DB)";  : "${DEV_DB:=atcraft_erp}"
MAIN_USER="$(env_value main POSTGRES_USER)"; : "${MAIN_USER:=atcraft}"
DEV_USER="$(env_value dev POSTGRES_USER)";  : "${DEV_USER:=atcraft}"
MAIN_PW="$(env_value main POSTGRES_PASSWORD)"
DEV_PW="$(env_value dev POSTGRES_PASSWORD)"

dc_main() { docker compose -p "$MAIN_PROJECT" --env-file deploy/main.env -f docker-compose.yml -f docker-compose.prod.yml "$@"; }
dc_dev()  { docker compose -p "$DEV_PROJECT"  --env-file deploy/dev.env  -f docker-compose.yml -f docker-compose.prod.yml "$@"; }

printf '%s\n' "${C_BOLD}Deployment isolation${C_RESET}"
printf '  deployment 1: %s on 127.0.0.1:%s\n' "$MAIN_PROJECT" "$MAIN_PORT"
printf '  deployment 2: %s on 127.0.0.1:%s\n' "$DEV_PROJECT" "$DEV_PORT"

# =============================================================================
section "1. Distinct infrastructure"
# The names below are derived from the project name by Compose. If any of them
# collide, the two deployments are one deployment and every check after this is
# meaningless - so this runs first.

if [ "$MAIN_PROJECT" != "$DEV_PROJECT" ]; then
  pass "project names differ ($MAIN_PROJECT / $DEV_PROJECT)"
else
  fail "both deployments use project name '$MAIN_PROJECT' - they share everything"
fi

for vol in postgres-data redis-data storage-data; do
  m="${MAIN_PROJECT}_${vol}"; d="${DEV_PROJECT}_${vol}"
  if docker volume inspect "$m" >/dev/null 2>&1 && docker volume inspect "$d" >/dev/null 2>&1; then
    mp="$(docker volume inspect -f '{{.Mountpoint}}' "$m")"
    dp="$(docker volume inspect -f '{{.Mountpoint}}' "$d")"
    if [ "$mp" != "$dp" ]; then
      pass "$vol: two volumes, two mountpoints"
    else
      fail "$vol: both deployments mount $mp"
    fi
  else
    skip "$vol: one or both volumes do not exist yet"
  fi
done

MAIN_NET="${MAIN_PROJECT}_internal"
DEV_NET="${DEV_PROJECT}_internal"
if docker network inspect "$MAIN_NET" >/dev/null 2>&1 && docker network inspect "$DEV_NET" >/dev/null 2>&1; then
  msub="$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$MAIN_NET")"
  dsub="$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$DEV_NET")"
  if [ "$msub" != "$dsub" ]; then
    pass "separate bridge networks ($msub / $dsub)"
  else
    fail "both deployments are on subnet $msub"
  fi
  # A container attached to both networks would bridge them regardless of
  # everything above. This is the failure mode a later "just add it to both
  # networks for convenience" change introduces.
  shared="$(docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' "$MAIN_NET" | tr ' ' '\n' | sort -u > /tmp/iso-main.$$
            docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' "$DEV_NET" | tr ' ' '\n' | sort -u > /tmp/iso-dev.$$
            comm -12 /tmp/iso-main.$$ /tmp/iso-dev.$$ | tr -d ' ')"
  rm -f /tmp/iso-main.$$ /tmp/iso-dev.$$
  if [ -z "$shared" ]; then
    pass "no container is attached to both networks"
  else
    fail "attached to both networks: $shared"
  fi
else
  skip "networks not both present - are both deployments up?"
fi

# =============================================================================
section "2. Deployment 1 cannot reach deployment 2's database"
# Attempted from inside deployment 1's own backend container, which is where an
# application bug, a bad migration target or a compromised dependency would run
# from. Reasoning about the compose file proves nothing here.

# The hostname `postgres` must resolve to deployment 1's own cluster and to
# nothing else. This is the crossing that matters most: both deployments use
# the same service names, so a shared network would make this succeed silently
# and every write would land in the wrong database.
#
# Deployment 2's container names are derived from its project name, never
# typed: deploy.sh accepts any COMPOSE_PROJECT_NAME that mentions "dev", so a
# hard-coded "atcraft-dev-postgres-1" would, for a project named anything else,
# resolve nowhere from either side and let "nothing resolved" pass as evidence.
# The same probe therefore runs first from inside deployment 2 as a positive
# control: its own postgres name must resolve there, or the negative result
# from deployment 1 proves nothing and is reported as a skip, not a pass.
#
# Prints PROBED, then one line per name that resolved. A missing PROBED line
# means the probe itself did not run (container down, exec refused), which is
# a different fact from "nothing resolved" and must not read as one.
resolve_dev_names() {   # $1 = main|dev: whose backend container asks
  "dc_$1" exec -T -e T_PROJECT="$DEV_PROJECT" backend python - <<'PY' 2>/dev/null
import os, socket
project = os.environ["T_PROJECT"]
print("PROBED")
# Compose v2 names containers <project>-<service>-N; v1 used underscores.
for service in ("postgres", "redis"):
    for host in (f"{project}-{service}-1", f"{project}_{service}_1"):
        try:
            socket.getaddrinfo(host, None)
        except socket.gaierror:
            continue
        print(host)
PY
}

control="$(resolve_dev_names dev)"
case "$control" in
  PROBED*) ;;
  *) control="" ;;
esac
control_names="$(printf '%s\n' "$control" | sed '1d' | tr '\n' ' ' | sed 's/ $//')"
case " $control_names " in
  *" ${DEV_PROJECT}-postgres-1 "*|*" ${DEV_PROJECT}_postgres_1 "*)
    note "positive control: inside deployment 2 these resolve: $control_names"
    crossed="$(resolve_dev_names main)"
    case "$crossed" in
      PROBED)
        pass "deployment 2's container names do not resolve from inside deployment 1"
        note "Compose puts DNS for a project on that project's network only."
        ;;
      PROBED*)
        fail "deployment 1 resolved deployment 2's container name(s): $(printf '%s\n' "$crossed" | sed '1d' | tr '\n' ' ')"
        ;;
      *)
        skip "could not run the name probe inside deployment 1's backend - is it up?"
        ;;
    esac
    ;;
  *)
    if [ -z "$control" ]; then
      skip "could not run the name probe inside deployment 2's backend - is it up?"
    else
      skip "deployment 2's own postgres name (${DEV_PROJECT}-postgres-1) does not resolve inside deployment 2 - a negative result from deployment 1 would prove nothing"
    fi
    ;;
esac

# Reaching the other cluster by IP, which is what remains when DNS is not the
# obstacle. Docker's default bridge policy blocks inter-network traffic; this
# checks that the policy is actually in force rather than assumed.
DEV_PG_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
             "$(dc_dev ps -q postgres 2>/dev/null)" 2>/dev/null | awk '{print $1}')"
if [ -n "$DEV_PG_IP" ]; then
  note "deployment 2's postgres is at $DEV_PG_IP"
  if dc_main exec -T backend python - "$DEV_PG_IP" <<'PY' 2>/dev/null
import socket, sys
ip = sys.argv[1]
s = socket.socket()
s.settimeout(4)
try:
    s.connect((ip, 5432))
except OSError:
    sys.exit(0)      # refused or timed out: isolated
finally:
    s.close()
sys.exit(1)          # a TCP connection opened across the boundary
PY
  then
    pass "deployment 1 cannot open a TCP connection to $DEV_PG_IP:5432"
  else
    fail "deployment 1 opened a socket to deployment 2's postgres by IP"
  fi
else
  skip "could not determine deployment 2's postgres IP"
fi

# And with the correct credentials in hand. If the network held, the password
# is irrelevant - which is the property worth having, because a password can
# leak and a network boundary cannot be phished.
if [ -n "$DEV_PW" ] && [ -n "$DEV_PG_IP" ]; then
  if dc_main exec -T -e T_PW="$DEV_PW" -e T_IP="$DEV_PG_IP" -e T_DB="$DEV_DB" -e T_USER="$DEV_USER" \
       backend python - <<'PY' 2>/dev/null
import asyncio, os, sys
import asyncpg

async def main() -> int:
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=os.environ["T_IP"], port=5432,
                user=os.environ["T_USER"], password=os.environ["T_PW"],
                database=os.environ["T_DB"],
            ),
            timeout=6,
        )
    except Exception:
        return 0          # could not connect at all
    rows = await conn.fetchval("SELECT count(*) FROM companies")
    await conn.close()
    print(f"READ {rows} companies from the other deployment")
    return 1

sys.exit(asyncio.run(main()))
PY
  then
    pass "deployment 1 cannot connect even holding deployment 2's real password"
    note "The boundary is the network, not the credential."
  else
    fail "deployment 1 read deployment 2's database using its password"
  fi
else
  skip "no password or IP available for the credentialled attempt"
fi

# The same crossing in the other direction. Isolation is not symmetric by
# construction - one extra `networks:` entry breaks exactly one direction.
if [ -n "$MAIN_PW" ]; then
  MAIN_PG_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
                "$(dc_main ps -q postgres 2>/dev/null)" 2>/dev/null | awk '{print $1}')"
  if [ -n "$MAIN_PG_IP" ]; then
    if dc_dev exec -T -e T_PW="$MAIN_PW" -e T_IP="$MAIN_PG_IP" -e T_DB="$MAIN_DB" -e T_USER="$MAIN_USER" \
         backend python - <<'PY' 2>/dev/null
import asyncio, os, sys
import asyncpg

async def main() -> int:
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                host=os.environ["T_IP"], port=5432,
                user=os.environ["T_USER"], password=os.environ["T_PW"],
                database=os.environ["T_DB"],
            ),
            timeout=6,
        )
    except Exception:
        return 0
    rows = await conn.fetchval("SELECT count(*) FROM companies")
    await conn.close()
    print(f"READ {rows} companies from the other deployment")
    return 1

sys.exit(asyncio.run(main()))
PY
    then
      pass "deployment 2 cannot reach deployment 1's database either"
    else
      fail "deployment 2 read deployment 1's database"
    fi
  else
    skip "could not determine deployment 1's postgres IP"
  fi
fi

# =============================================================================
section "3. Deployment 1 cannot reach deployment 2's Redis"
# Redis holds sessions, cache entries and the Celery queues. A shared instance
# means one deployment's worker picking up the other's jobs - which would run
# real business logic against the wrong database.

DEV_REDIS_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
                "$(dc_dev ps -q redis 2>/dev/null)" 2>/dev/null | awk '{print $1}')"
if [ -n "$DEV_REDIS_IP" ]; then
  if dc_main exec -T backend python - "$DEV_REDIS_IP" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(4)
try:
    s.connect((sys.argv[1], 6379))
except OSError:
    sys.exit(0)
finally:
    s.close()
sys.exit(1)
PY
  then
    pass "deployment 1 cannot open deployment 2's Redis at $DEV_REDIS_IP:6379"
  else
    fail "deployment 1 reached deployment 2's Redis - the Celery queues are shared"
  fi
else
  skip "could not determine deployment 2's redis IP"
fi

# =============================================================================
section "4. Neither database is reachable from the host"
# A published port re-opens every boundary above: anything on the box, and
# anything that can reach the box, addresses the database directly. The prod
# overlay publishes no database port at all, and this is what proves it.

# Read the binding off the container rather than asking compose. `docker
# compose port` prints "invalidIP:0" instead of nothing when a port is
# unpublished on some platforms, so an emptiness test reports a leak that is
# not there - which is how this check first "failed" against two deployments
# that were correctly isolated. The container's own NetworkSettings.Ports is
# unambiguous: null means nothing is published.
host_bindings() {
  docker inspect -f '{{range $p, $conf := .NetworkSettings.Ports}}{{range $conf}}{{$p}}->{{.HostIp}}:{{.HostPort}} {{end}}{{end}}' "$1" 2>/dev/null
}

for svc in postgres redis; do
  leaked=""
  for project_dc in main dev; do
    if [ "$project_dc" = "main" ]; then cid="$(dc_main ps -q "$svc" 2>/dev/null)"; else cid="$(dc_dev ps -q "$svc" 2>/dev/null)"; fi
    [ -n "$cid" ] || continue
    bound="$(host_bindings "$cid")"
    [ -n "$bound" ] && leaked="$leaked $project_dc:$bound"
  done
  if [ -z "$leaked" ]; then
    pass "$svc publishes no host port in either deployment"
  else
    fail "$svc is published on the host:$leaked"
  fi
done

# The one published port per deployment must be loopback-bound. On 0.0.0.0 the
# whole application is served unencrypted, bypassing the host nginx and its
# TLS, to anything that can reach the box.
for pair in "main $MAIN_PROJECT" "dev $DEV_PROJECT"; do
  set -- $pair
  name="$1"
  if [ "$name" = "main" ]; then cid="$(dc_main ps -q nginx 2>/dev/null)"; else cid="$(dc_dev ps -q nginx 2>/dev/null)"; fi
  [ -n "$cid" ] || { skip "$name: nginx container not found"; continue; }
  bound="$(host_bindings "$cid")"
  case "$bound" in
    *0.0.0.0:*|*"[::]":*) fail "$name: nginx is published on all interfaces ($bound)" ;;
    *127.0.0.1:*)         pass "$name: nginx published on loopback only ($bound)" ;;
    "")                   fail "$name: nginx publishes no port - the host nginx has nothing to proxy to" ;;
    *)                    fail "$name: unexpected nginx binding ($bound)" ;;
  esac
done

# =============================================================================
section "5. Each HTTP port serves its own deployment"

for pair in "main $MAIN_PORT" "dev $DEV_PORT"; do
  set -- $pair
  name="$1"; port="$2"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${port}/health/ready" || echo 000)"
  if [ "$code" = "200" ]; then
    pass "$name: 127.0.0.1:$port/health/ready -> 200"
  else
    fail "$name: 127.0.0.1:$port/health/ready -> $code"
  fi
done

# Deployment 1's port must answer from deployment 1's database. Proven with a
# marker row rather than a health check: both stacks are healthy either way,
# and a health check cannot tell you which database answered.
#
# Runs regardless of earlier failures. Gating it on a clean run meant the one
# check that actually reads both databases was skipped precisely when something
# looked wrong - and a skip is not a pass. It writes one row to a table it
# creates and drops.
if true; then
  MARKER="isolation-probe-$$"
  dc_main exec -T postgres psql -U "$MAIN_USER" -d "$MAIN_DB" -q \
    -c "CREATE TABLE IF NOT EXISTS _isolation_probe(tag text primary key);" \
    -c "INSERT INTO _isolation_probe VALUES ('$MARKER') ON CONFLICT DO NOTHING;" >/dev/null 2>&1
  seen_in_dev="$(dc_dev exec -T postgres psql -U "$DEV_USER" -d "$DEV_DB" -tAc \
    "SELECT count(*) FROM _isolation_probe WHERE tag='$MARKER';" 2>/dev/null | tr -d '[:space:]')"
  if [ "${seen_in_dev:-0}" = "0" ]; then
    pass "a row written to deployment 1's database is absent from deployment 2's"
    note "Marker: $MARKER"
  else
    fail "the marker row appeared in BOTH databases - they are the same database"
  fi
  dc_main exec -T postgres psql -U "$MAIN_USER" -d "$MAIN_DB" -q \
    -c "DROP TABLE IF EXISTS _isolation_probe;" >/dev/null 2>&1
  dc_dev exec -T postgres psql -U "$DEV_USER" -d "$DEV_DB" -q \
    -c "DROP TABLE IF EXISTS _isolation_probe;" >/dev/null 2>&1
fi

# =============================================================================
section "6. A token from one deployment is refused by the other"
# The last boundary, and the one that survives a network misconfiguration: even
# if the two stacks could see each other, a session minted by the dev
# deployment must not authenticate against the client's data.

MAIN_JWT="$(env_value main JWT_SECRET)"
DEV_JWT="$(env_value dev JWT_SECRET)"
if [ -z "$MAIN_JWT" ] || [ -z "$DEV_JWT" ]; then
  skip "one of the JWT secrets is empty"
elif [ "$MAIN_JWT" = "$DEV_JWT" ]; then
  fail "both deployments share JWT_SECRET - a dev token is valid in production"
else
  pass "the two deployments sign tokens with different secrets"
  m_aud="$(env_value main JWT_AUDIENCE)"; d_aud="$(env_value dev JWT_AUDIENCE)"
  if [ -n "$m_aud" ] && [ "$m_aud" != "$d_aud" ]; then
    pass "and declare different audiences ($m_aud / $d_aud)"
    note "So a cross-deployment token is rejected as wrong-audience, not merely unverifiable."
  else
    skip "JWT_AUDIENCE is shared or unset - the differing secret still separates them"
  fi
fi

# =============================================================================
section "7. A session from one deployment, replayed against the other"
# Section 6 compares the inputs; this observes the outcome. They are not the
# same claim - differing secrets prove nothing if a deployment were configured
# to skip verification, and only a real request can show what the API does with
# a foreign token.
#
# Needs the two administrator passwords, which nothing stores in plaintext, so
# it is opt-in:
#
#   ATCRAFT_MAIN_PW='...' ATCRAFT_DEV_PW='...' ./scripts/verify-isolation.sh
#
# The passwords are read from the environment and never printed or logged.

if [ -z "${ATCRAFT_MAIN_PW:-}" ] || [ -z "${ATCRAFT_DEV_PW:-}" ]; then
  skip "set ATCRAFT_MAIN_PW and ATCRAFT_DEV_PW to replay a real session across deployments"
else
  login_token() {   # $1 = port, $2 = password
    curl -s --max-time 15 -X POST "http://127.0.0.1:$1/api/v1/auth/login" \
      -H 'Content-Type: application/json' \
      -d "{\"email\":\"${ATCRAFT_ADMIN_EMAIL:-admin@atcraft.example}\",\"password\":\"$2\"}" \
      | python -c "import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
payload = d.get('data')
if isinstance(payload, list):
    payload = payload[0] if payload else {}
print((payload or {}).get('access_token', ''))" 2>/dev/null
  }
  probe_me() {      # $1 = port, $2 = token
    curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
      -H "Authorization: Bearer $2" "http://127.0.0.1:$1/api/v1/auth/me"
  }

  main_tok="$(login_token "$MAIN_PORT" "$ATCRAFT_MAIN_PW")"
  dev_tok="$(login_token "$DEV_PORT" "$ATCRAFT_DEV_PW")"

  if [ -z "$main_tok" ] || [ -z "$dev_tok" ]; then
    skip "could not sign in to both deployments - check the passwords"
  else
    # The own-deployment probes must pass first. Without them, a token that is
    # rejected everywhere - a typo, an expired session - reads as perfect
    # isolation.
    own_main="$(probe_me "$MAIN_PORT" "$main_tok")"
    own_dev="$(probe_me "$DEV_PORT" "$dev_tok")"
    if [ "$own_main" = "200" ] && [ "$own_dev" = "200" ]; then
      pass "each deployment accepts its own session (200 / 200)"
      cross_a="$(probe_me "$MAIN_PORT" "$dev_tok")"
      cross_b="$(probe_me "$DEV_PORT" "$main_tok")"
      if [ "$cross_a" = "401" ] && [ "$cross_b" = "401" ]; then
        pass "and refuses the other's (401 / 401)"
      else
        fail "a cross-deployment token was not refused (dev->main $cross_a, main->dev $cross_b)"
      fi
    else
      skip "own-deployment sign-in did not return 200 (main $own_main, dev $own_dev) - cross test would be meaningless"
    fi
  fi
fi

# =============================================================================
printf '\n%s\n' "${C_BOLD}Result${C_RESET}"
printf '  passed %s   failed %s   skipped %s\n' "$PASSED" "$FAILED" "$SKIPPED"
if [ "$FAILED" -gt 0 ]; then
  printf '%s\n' "${C_RED}Deployments are NOT isolated. Do not hand this over.${C_RESET}"
  exit 1
fi
if [ "$SKIPPED" -gt 0 ]; then
  printf '%s\n' "${C_DIM}Some checks were skipped - a skip is not a pass. Bring both deployments up and re-run.${C_RESET}"
fi
printf '%s\n' "${C_GREEN}Deployments are independent: separate volumes, networks, databases, Redis and signing keys.${C_RESET}"
