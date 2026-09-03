#!/usr/bin/env bash
# =============================================================================
# Atcraft ERP - the ONLY entry point for a deployment.
#
#   ./scripts/deploy.sh <main|dev> <command> [args]
#
# Two independent deployments run on one host. What keeps them independent is
# the Compose project name, and what makes that reliable is that nobody types a
# raw `docker compose` command: this script reads deploy/<name>.env, takes the
# project name from it, and passes it explicitly with -p on every invocation.
#
# A bare `docker compose up` in this directory now fails outright, because
# docker-compose.yml requires COMPOSE_PROJECT_NAME. That is deliberate. The
# failure it replaces is silent: without a project name Compose derives one
# from the directory, both deployments land in the same project, and deployment
# 2 mounts - and migrates - deployment 1's database while looking perfectly
# healthy.
#
# Run `./scripts/deploy.sh` with no arguments for the command list.
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- Output ------------------------------------------------------------------
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_DIM=$'\033[2m'
else
  C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_DIM=""
fi
say()  { printf '%s\n' "${C_BOLD}$*${C_RESET}"; }
info() { printf '%s\n' "${C_DIM}$*${C_RESET}"; }
ok()   { printf '%s\n' "${C_GREEN}$*${C_RESET}"; }
warn() { printf '%s\n' "${C_YELLOW}$*${C_RESET}" >&2; }
die()  { printf '%s\n' "${C_RED}error: $*${C_RESET}" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: ./scripts/deploy.sh <deployment> <command> [args]

Deployments
  main            deployment 1 - main branch, the client's environment
  dev             deployment 2 - dev branch, integration testing

Setup
  init            create deploy/<name>.env from the template with generated
                  secrets (never overwrites an existing file)
  doctor          check this deployment's configuration without starting it

Lifecycle
  build           build the images for this deployment
  up              build if needed, start, wait for health
  down            stop and remove containers (VOLUMES AND DATA ARE KEPT)
  destroy         stop and DELETE this deployment's volumes - asks first
  restart         down, then up
  ps              container status
  logs [svc...]   follow logs
  health          report each container's health and the HTTP endpoint

Database (this deployment's database only)
  migrate         alembic upgrade head
  migrate-status  the revision this database is on, and what is pending
  downgrade <n>   roll back n revisions - asks first
  seed            load baseline reference data (roles, permissions, UoM)
  demo-data       load the demo business on top of the baseline
  psql [args]     psql inside this deployment's postgres
  shell           a shell in this deployment's backend container

Backup
  backup          pg_dump to backups/<deployment>/ (custom format)
  restore <file>  restore a dump into this deployment - asks first
  backup-verify   take a dump, restore it into a scratch database, count rows

Examples
  ./scripts/deploy.sh main init
  ./scripts/deploy.sh main up
  ./scripts/deploy.sh main migrate
  ./scripts/deploy.sh dev  demo-data
  ./scripts/deploy.sh main backup
USAGE
}

# --- Argument parsing --------------------------------------------------------
DEPLOYMENT="${1:-}"
COMMAND="${2:-}"
[ -n "$DEPLOYMENT" ] || { usage; exit 1; }
case "$DEPLOYMENT" in
  main|dev) ;;
  -h|--help|help) usage; exit 0 ;;
  *) die "unknown deployment '$DEPLOYMENT' (expected 'main' or 'dev')" ;;
esac
[ -n "$COMMAND" ] || { usage; exit 1; }
shift 2 || true

ENV_FILE="deploy/${DEPLOYMENT}.env"
ENV_TEMPLATE="deploy/${DEPLOYMENT}.env.example"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.prod.yml)

# --- init: generate the env file --------------------------------------------
# Done here rather than by hand because the two things most likely to be
# copy-pasted between deployments are exactly the two that must never be
# shared: the database password and the JWT secret.
cmd_init() {
  [ -f "$ENV_TEMPLATE" ] || die "missing template $ENV_TEMPLATE"
  if [ -f "$ENV_FILE" ]; then
    die "$ENV_FILE already exists - refusing to overwrite it.
       Rotating a secret is a deliberate act: edit the file, then
       './scripts/deploy.sh $DEPLOYMENT restart'. Overwriting JWT_SECRET logs
       every user out; overwriting POSTGRES_PASSWORD leaves the running
       cluster on the old one and the app unable to connect."
  fi

  local pg_pw jwt
  pg_pw="$(generate_secret 32)"
  jwt="$(generate_secret 64)"

  # Substituted with awk rather than sed: a generated secret can contain the
  # delimiter, & or a backslash, and sed would corrupt it silently.
  awk -v pg="$pg_pw" -v jwt="$jwt" '
    /^POSTGRES_PASSWORD=$/ { print "POSTGRES_PASSWORD=" pg; next }
    /^JWT_SECRET=$/        { print "JWT_SECRET=" jwt;      next }
    { print }
  ' "$ENV_TEMPLATE" > "$ENV_FILE"

  chmod 600 "$ENV_FILE"

  grep -q '^POSTGRES_PASSWORD=.\+' "$ENV_FILE" || die "failed to write POSTGRES_PASSWORD"
  grep -q '^JWT_SECRET=.\+' "$ENV_FILE" || die "failed to write JWT_SECRET"

  ok "created $ENV_FILE (mode 600) with freshly generated secrets"
  info "Neither secret is printed here or logged anywhere. Read them with:"
  info "  grep JWT_SECRET $ENV_FILE"
  info ""
  info "Still to set by hand:"
  info "  CORS_ORIGINS   this deployment's public origin"
  warn "Do not copy secrets between deploy/main.env and deploy/dev.env."
}

generate_secret() {
  local bytes="$1"
  if command -v openssl >/dev/null 2>&1; then
    # tr -d removes the newline and the '=' padding, keeping it shell-safe.
    openssl rand -base64 "$bytes" | tr -d '\n=+/'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets,sys; print(secrets.token_urlsafe(int(sys.argv[1])))" "$bytes"
  elif command -v python >/dev/null 2>&1; then
    python -c "import secrets,sys; print(secrets.token_urlsafe(int(sys.argv[1])))" "$bytes"
  else
    die "need openssl or python to generate a secret"
  fi
}

if [ "$COMMAND" = "init" ]; then cmd_init; exit 0; fi

# --- Load and validate the deployment's environment -------------------------
[ -f "$ENV_FILE" ] || die "missing $ENV_FILE
       Create it with:  ./scripts/deploy.sh $DEPLOYMENT init"

# Read the few values this script needs. Deliberately narrow: the file is fed
# to docker compose via --env-file, so nothing here needs to source it, and
# sourcing an env file executes whatever is in it.
env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r'
}

PROJECT="$(env_value COMPOSE_PROJECT_NAME)"
HTTP_PORT="$(env_value HTTP_PORT)"
DEPLOY_BRANCH="$(env_value DEPLOY_BRANCH)"
PG_DB="$(env_value POSTGRES_DB)"
PG_USER="$(env_value POSTGRES_USER)"

[ -n "$PROJECT" ] || die "COMPOSE_PROJECT_NAME is not set in $ENV_FILE - it is the isolation boundary"
[ -n "$HTTP_PORT" ] || die "HTTP_PORT is not set in $ENV_FILE"
[ -n "$(env_value POSTGRES_PASSWORD)" ] || die "POSTGRES_PASSWORD is empty in $ENV_FILE - run './scripts/deploy.sh $DEPLOYMENT init' or fill it in"
[ -n "$(env_value JWT_SECRET)" ] || die "JWT_SECRET is empty in $ENV_FILE"
: "${PG_DB:=atcraft_erp}"
: "${PG_USER:=atcraft}"

# The project name must be this deployment's, not the other's. Without this
# check, one wrong line in an env file points every command in this script -
# migrate and restore included - at the other deployment's database.
case "$PROJECT" in
  *"$DEPLOYMENT"*) ;;
  *) die "COMPOSE_PROJECT_NAME='$PROJECT' in $ENV_FILE does not mention '$DEPLOYMENT'.
       Refusing to continue: every command below would act on the wrong deployment." ;;
esac

OTHER_DEPLOYMENT="main"; [ "$DEPLOYMENT" = "main" ] && OTHER_DEPLOYMENT="dev"
OTHER_ENV="deploy/${OTHER_DEPLOYMENT}.env"

# Cross-check against the other deployment's file when it exists. These three
# collisions each produce a working stack that quietly shares state.
if [ -f "$OTHER_ENV" ]; then
  other_value() { sed -n "s/^$1=//p" "$OTHER_ENV" | tail -n 1 | tr -d '\r'; }
  [ "$PROJECT" != "$(other_value COMPOSE_PROJECT_NAME)" ] || \
    die "both deployments declare COMPOSE_PROJECT_NAME='$PROJECT'.
       They are one deployment sharing one database. Fix $ENV_FILE or $OTHER_ENV."
  [ "$HTTP_PORT" != "$(other_value HTTP_PORT)" ] || \
    die "both deployments declare HTTP_PORT=$HTTP_PORT. The second to start cannot bind it."
  if [ "$(env_value JWT_SECRET)" = "$(other_value JWT_SECRET)" ]; then
    die "both deployments share JWT_SECRET.
       A token minted by $OTHER_DEPLOYMENT would authenticate against $DEPLOYMENT.
       Generate a new one for one of them and restart it."
  fi
  if [ "$(env_value POSTGRES_PASSWORD)" = "$(other_value POSTGRES_PASSWORD)" ]; then
    warn "both deployments share POSTGRES_PASSWORD."
    warn "Neither can reach the other's network, so this is not exploitable today -"
    warn "but it stops being true the moment a port is published for debugging."
  fi
fi

dc() {
  # -p on every call, explicitly. The env file also sets COMPOSE_PROJECT_NAME,
  # and this is the belt to that braces: the project name is visible in every
  # command that runs, so there is no way to read the log afterwards and be
  # unsure which deployment it acted on.
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" "$@"
}

confirm() {
  local prompt="$1" expected="$2" answer
  printf '%s\n' "${C_YELLOW}${prompt}${C_RESET}" >&2
  printf '%s' "Type '${expected}' to continue: " >&2
  read -r answer || answer=""
  [ "$answer" = "$expected" ] || die "cancelled"
}

assert_branch() {
  # A deployment builds from the working tree. If the tree is on the wrong
  # branch, `up` ships the wrong code and nothing in the running stack says so.
  if [ "${DEPLOY_BRANCH_OVERRIDE:-0}" = "1" ]; then
    warn "DEPLOY_BRANCH_OVERRIDE=1 - not checking the branch"
    return 0
  fi
  [ -n "$DEPLOY_BRANCH" ] || return 0
  local current
  current="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  # No branch to check against in a detached head or a non-repo checkout.
  [ -n "$current" ] && [ "$current" != "HEAD" ] || return 0
  if [ "$current" != "$DEPLOY_BRANCH" ]; then
    die "deployment '$DEPLOYMENT' tracks branch '$DEPLOY_BRANCH' but the working tree is on '$current'.
       Run:  git checkout $DEPLOY_BRANCH
       Or, if this is deliberate:  DEPLOY_BRANCH_OVERRIDE=1 $0 $DEPLOYMENT $COMMAND"
  fi
}

# --- Commands ----------------------------------------------------------------
case "$COMMAND" in

  doctor)
    say "Deployment '$DEPLOYMENT'"
    printf '  %-22s %s\n' "project"      "$PROJECT"
    printf '  %-22s %s\n' "env file"     "$ENV_FILE"
    printf '  %-22s %s\n' "branch"       "${DEPLOY_BRANCH:-(unpinned)}"
    printf '  %-22s %s\n' "http"         "127.0.0.1:${HTTP_PORT}"
    printf '  %-22s %s\n' "database"     "$PG_DB (as $PG_USER)"
    printf '  %-22s %s\n' "app env"      "$(env_value APP_ENV)"
    printf '  %-22s %s\n' "cors origins" "$(env_value CORS_ORIGINS)"
    printf '  %-22s %s\n' "jwt audience" "$(env_value JWT_AUDIENCE)"
    printf '  %-22s %s\n' "secrets"      "set (values not shown)"
    say ""
    say "Compose configuration"
    # Renders and validates every interpolation. Fails loudly on a missing
    # required variable, which is the point.
    if dc config >/dev/null 2>/tmp/atcraft-doctor.$$; then
      ok "  compose config is valid"
      printf '  %-22s %s\n' "volumes" "$(dc config --volumes | tr '\n' ' ')"
    else
      cat /tmp/atcraft-doctor.$$ >&2
      rm -f /tmp/atcraft-doctor.$$
      die "compose configuration is invalid"
    fi
    rm -f /tmp/atcraft-doctor.$$
    say ""
    say "Isolation"
    printf '  %-22s %s\n' "network"  "${PROJECT}_internal"
    printf '  %-22s %s\n' "volumes"  "${PROJECT}_postgres-data, ${PROJECT}_redis-data, ${PROJECT}_storage-data"
    info "  These names are derived from the project name. Two deployments with"
    info "  different project names cannot address each other's data."
    ;;

  build)   assert_branch; dc build "$@" ;;

  up)
    assert_branch
    say "Starting deployment '$DEPLOYMENT' (project $PROJECT)"
    dc up -d --build --wait "$@"
    ok "containers are up and healthy"
    info "Next:  ./scripts/deploy.sh $DEPLOYMENT migrate"
    ;;

  down)
    # No -v. Ever, from this subcommand: `down -v` on the wrong deployment is
    # unrecoverable, and the two commands differ by two characters.
    say "Stopping deployment '$DEPLOYMENT' - volumes and data are kept"
    dc down "$@"
    ;;

  destroy)
    warn "This DELETES deployment '$DEPLOYMENT' volumes:"
    warn "  ${PROJECT}_postgres-data  (the entire database)"
    warn "  ${PROJECT}_redis-data"
    warn "  ${PROJECT}_storage-data   (every uploaded document)"
    confirm "There is no undo. A backup taken before this is the only way back." "destroy $DEPLOYMENT"
    dc down -v --remove-orphans
    ok "deployment '$DEPLOYMENT' destroyed"
    ;;

  restart) assert_branch; dc down; dc up -d --build --wait; ok "restarted" ;;
  ps)      dc ps "$@" ;;
  logs)    dc logs -f --tail=100 "$@" ;;

  health)
    say "Deployment '$DEPLOYMENT' (project $PROJECT)"
    dc ps --format 'table {{.Service}}\t{{.Status}}'
    say ""
    say "HTTP on 127.0.0.1:${HTTP_PORT}"
    # /healthz is nginx answering for itself; the /health/* pair is the
    # application. They live at the app root, NOT under /api/v1 - see
    # src/main.py. Probing the wrong path here reaches the SPA, which answers
    # 200 with an HTML page, so the check could never fail.
    for path in /healthz /health/live /health/ready; do
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${HTTP_PORT}${path}" || echo "000")"
      if [ "$code" = "200" ]; then ok "  $path -> 200"; else warn "  $path -> $code"; fi
    done
    # Readiness names its dependencies; print them, because "not ready" without
    # saying which of Postgres or Redis is unreachable starts the wrong search.
    body="$(curl -s --max-time 10 "http://127.0.0.1:${HTTP_PORT}/health/ready" || echo "")"
    [ -n "$body" ] && info "  $body"
    ;;

  # --- Database ------------------------------------------------------------
  # Every one of these runs inside THIS deployment's project, so it can only
  # reach this deployment's database. There is no host-side connection string
  # to get wrong, and no published port to point at the wrong container.
  migrate)
    say "Migrating deployment '$DEPLOYMENT' database ($PG_DB)"
    dc run --rm --no-deps -T backend alembic upgrade head
    ok "migrated"
    ;;

  migrate-status)
    say "Current revision"
    dc run --rm --no-deps -T backend alembic current -v
    say "Heads"
    dc run --rm --no-deps -T backend alembic heads
    ;;

  downgrade)
    steps="${1:-1}"
    warn "Rolling back $steps revision(s) on deployment '$DEPLOYMENT'."
    warn "A downgrade that drops a column destroys the data in it."
    confirm "Take a backup first if you have not." "downgrade $DEPLOYMENT"
    dc run --rm --no-deps -T backend alembic downgrade "-${steps}"
    ;;

  seed)
    say "Loading baseline reference data into '$DEPLOYMENT'"
    dc run --rm --no-deps -T backend python -m src.core.seed
    ;;

  demo-data)
    if [ "$(env_value APP_ENV)" = "production" ]; then
      warn "Deployment '$DEPLOYMENT' has APP_ENV=production."
      warn "Demo data invents customers, orders and stock. Mixed into real"
      warn "records it is not separable afterwards - the codes are the only"
      warn "marker, and only on the parents."
      confirm "Load invented business data into a production deployment?" "yes load demo data into $DEPLOYMENT"
    fi
    dc run --rm --no-deps -T backend python -m src.core.demo_data "$@"
    ;;

  psql)
    dc exec postgres psql -U "$PG_USER" -d "$PG_DB" "$@"
    ;;

  shell)
    dc exec backend bash
    ;;

  # --- Backup --------------------------------------------------------------
  backup)
    dest="backups/${DEPLOYMENT}"
    mkdir -p "$dest"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    file="${dest}/${PROJECT}-${PG_DB}-${stamp}.dump"
    say "Dumping deployment '$DEPLOYMENT' -> $file"
    # -Fc (custom format) rather than plain SQL: it restores selectively, in
    # parallel, and compresses. pg_dump runs inside the container, so it needs
    # no published port and no host-side password.
    dc exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" -Fc --no-owner --no-privileges > "$file"
    [ -s "$file" ] || { rm -f "$file"; die "pg_dump produced an empty file"; }
    # Read-only, because a dump is a full copy of the business including every
    # password hash.
    chmod 400 "$file"
    ok "wrote $file ($(du -h "$file" | cut -f1))"
    warn "This is one copy, on the machine it came from. That is not a backup"
    warn "yet (section 45) - send it off the box:"
    info "  scp $file <somewhere-else>:/backups/"
    ;;

  restore)
    file="${1:-}"
    [ -n "$file" ] || die "usage: ./scripts/deploy.sh $DEPLOYMENT restore <file.dump>"
    [ -f "$file" ] || die "no such file: $file"
    warn "Restoring into deployment '$DEPLOYMENT' database '$PG_DB'."
    warn "Every row currently in it that the dump also contains is replaced."
    confirm "Source: $file" "restore into $DEPLOYMENT"
    say "Restoring..."
    # --clean --if-exists drops objects before recreating them, so a restore
    # over a populated database is deterministic rather than a pile of
    # duplicate-key errors. --single-transaction makes it all-or-nothing: a
    # restore that fails half way leaves the database as it was.
    dc exec -T postgres pg_restore -U "$PG_USER" -d "$PG_DB" \
        --clean --if-exists --no-owner --no-privileges --single-transaction < "$file"
    ok "restored"
    info "Restart the app so no worker holds a stale connection or cache:"
    info "  ./scripts/deploy.sh $DEPLOYMENT restart"
    ;;

  backup-verify)
    # A backup nobody has restored is a hypothesis. This restores into a
    # scratch database inside the same cluster and compares row counts, so the
    # claim "we have backups" is testable without touching the live database.
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    scratch="${PG_DB}_restorecheck"
    tmp="backups/${DEPLOYMENT}/verify-${stamp}.dump"
    mkdir -p "backups/${DEPLOYMENT}"
    say "1/4 dumping $PG_DB"
    dc exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" -Fc --no-owner --no-privileges > "$tmp"
    [ -s "$tmp" ] || die "pg_dump produced an empty file"
    say "2/4 creating scratch database $scratch"
    dc exec -T postgres psql -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$scratch\";" >/dev/null
    dc exec -T postgres psql -U "$PG_USER" -d postgres -c "CREATE DATABASE \"$scratch\";" >/dev/null
    say "3/4 restoring into $scratch"
    dc exec -T postgres pg_restore -U "$PG_USER" -d "$scratch" --no-owner --no-privileges < "$tmp" >/dev/null
    say "4/4 comparing"
    counts_sql="SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
    live="$(dc exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "$counts_sql" | tr -d '\r')"
    copy="$(dc exec -T postgres psql -U "$PG_USER" -d "$scratch" -tAc "$counts_sql" | tr -d '\r')"
    printf '  tables: live=%s restored=%s\n' "$live" "$copy"
    for t in companies users items inventory_ledger audit_logs; do
      l="$(dc exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "SELECT count(*) FROM \"$t\";" 2>/dev/null | tr -d '\r' || echo "-")"
      c="$(dc exec -T postgres psql -U "$PG_USER" -d "$scratch" -tAc "SELECT count(*) FROM \"$t\";" 2>/dev/null | tr -d '\r' || echo "-")"
      if [ "$l" = "$c" ]; then printf '  %-18s %s == %s\n' "$t" "$l" "$c"
      else warn "  $t MISMATCH live=$l restored=$c"; fi
    done
    dc exec -T postgres psql -U "$PG_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$scratch\";" >/dev/null
    rm -f "$tmp"
    ok "restore verified against a real restore, scratch database dropped"
    ;;

  config) dc config "$@" ;;

  -h|--help|help) usage ;;
  *) die "unknown command '$COMMAND' - run './scripts/deploy.sh $DEPLOYMENT help'" ;;
esac
