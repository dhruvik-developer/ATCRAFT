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

# Secret files and database dumps must be private from the instant they are
# created, including failure paths before an explicit chmod can run.
umask 077

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
  dev             deployment 2 - develop branch, integration testing

Setup
  init            create deploy/<name>.env from the template with generated
                  secrets (never overwrites an existing file)
  doctor          check this deployment's configuration without starting it

Lifecycle
  build           build commit-addressed images from a clean working tree
  up              safe release: build, stop traffic, backup, migrate, start
  down            stop and remove containers (VOLUMES AND DATA ARE KEPT)
  destroy         stop and DELETE this deployment's volumes - asks first
  restart         re-run the guarded release: the same checks and steps as
                  'up' (clean tree, HEAD at upstream, rebuild, backup, migrate)
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
  shell [cmd...]  a shell in this deployment's backend container, or the
                  given command run inside it

Backup
  backup          pg_dump to backups/<deployment>/ (custom format)
  restore <file>  restore a database dump, migrate it, then resume traffic
  backup-verify   take a dump, restore it into a scratch database, count rows

Examples
  ./scripts/deploy.sh main init
  ./scripts/deploy.sh main up
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
# copy-pasted between deployments are exactly the credentials that must never
# be shared: the database password, JWT secret and credential-encryption key.
cmd_init() {
  [ -f "$ENV_TEMPLATE" ] || die "missing template $ENV_TEMPLATE"
  if [ -f "$ENV_FILE" ]; then
    die "$ENV_FILE already exists - refusing to overwrite it.
       Rotating a secret is a deliberate act: edit the file, then
       './scripts/deploy.sh $DEPLOYMENT restart'. Overwriting JWT_SECRET logs
       every user out; overwriting POSTGRES_PASSWORD leaves the running
       cluster on the old one and the app unable to connect."
  fi

  local pg_pw jwt credential_key
  pg_pw="$(generate_secret 32)"
  jwt="$(generate_secret 64)"
  credential_key="$(generate_secret 64)"

  # Substituted with awk rather than sed: a generated secret can contain the
  # delimiter, & or a backslash, and sed would corrupt it silently.
  awk -v pg="$pg_pw" -v jwt="$jwt" -v credential="$credential_key" '
    /^POSTGRES_PASSWORD=$/ { print "POSTGRES_PASSWORD=" pg; next }
    /^JWT_SECRET=$/        { print "JWT_SECRET=" jwt;      next }
    /^CREDENTIAL_ENCRYPTION_KEY=$/ {
      print "CREDENTIAL_ENCRYPTION_KEY=" credential; next
    }
    { print }
  ' "$ENV_TEMPLATE" > "$ENV_FILE"

  chmod 600 "$ENV_FILE"

  grep -q '^POSTGRES_PASSWORD=.\+' "$ENV_FILE" || die "failed to write POSTGRES_PASSWORD"
  grep -q '^JWT_SECRET=.\+' "$ENV_FILE" || die "failed to write JWT_SECRET"
  grep -q '^CREDENTIAL_ENCRYPTION_KEY=.\+' "$ENV_FILE" || die "failed to write CREDENTIAL_ENCRYPTION_KEY"

  ok "created $ENV_FILE (mode 600) with freshly generated secrets"
  info "No secret is printed here or logged anywhere. Inspect their presence without exposing values with:"
  info "  awk -F= '/^(JWT_SECRET|CREDENTIAL_ENCRYPTION_KEY)=/ { printf \"%s=<set, %d chars>\\n\", \$1, length(\$2) }' $ENV_FILE"
  info ""
  info "Still to set by hand (all of these ship as example.com placeholders):"
  info "  CORS_ORIGINS        this deployment's public origin, https://..."
  info "  ALLOWED_HOSTS       this deployment's public hostname"
  info "  PUBLIC_WEB_URL      the origin password-reset links point at"
  info "  EMAIL_FROM_ADDRESS  the sender address for outbound mail"
  info "  SMTP_HOST           the mail relay"
  info "  SMTP_USERNAME/SMTP_PASSWORD  only if the relay authenticates; both or neither"
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
JWT_SECRET_VALUE="$(env_value JWT_SECRET)"
CREDENTIAL_KEY_VALUE="$(env_value CREDENTIAL_ENCRYPTION_KEY)"

# Application images are addressed by the exact source revision, never by a
# mutable branch label such as "main". This script overrides any externally
# supplied IMAGE_TAG on every invocation.
SOURCE_REVISION="$(git rev-parse --verify HEAD 2>/dev/null || printf 'unknown')"
RELEASE_TAG="${SOURCE_REVISION:0:12}"

[ -n "$PROJECT" ] || die "COMPOSE_PROJECT_NAME is not set in $ENV_FILE - it is the isolation boundary"
[ -n "$HTTP_PORT" ] || die "HTTP_PORT is not set in $ENV_FILE"
[ -n "$(env_value POSTGRES_PASSWORD)" ] || die "POSTGRES_PASSWORD is empty in $ENV_FILE - run './scripts/deploy.sh $DEPLOYMENT init' or fill it in"
[ "${#JWT_SECRET_VALUE}" -ge 32 ] || die "JWT_SECRET must contain at least 32 characters"
[ "${#CREDENTIAL_KEY_VALUE}" -ge 32 ] || die "CREDENTIAL_ENCRYPTION_KEY must contain at least 32 characters"
# Length alone lets the .env.example placeholders through: they are long enough,
# and copied by hand they would sign every token with a string in a public repo.
for secret_value in "$JWT_SECRET_VALUE" "$CREDENTIAL_KEY_VALUE"; do
  case "$secret_value" in
    *[Cc][Hh][Aa][Nn][Gg][Ee][-_][Mm][Ee]*|*[Cc][Hh][Aa][Nn][Gg][Ee][Mm][Ee]*|*generate-a*|*[Pp][Ll][Aa][Cc][Ee][Hh][Oo][Ll][Dd][Ee][Rr]*)
      die "JWT_SECRET / CREDENTIAL_ENCRYPTION_KEY look like template placeholders - run './scripts/deploy.sh $DEPLOYMENT init' to generate real ones" ;;
  esac
done
[ "$JWT_SECRET_VALUE" != "$CREDENTIAL_KEY_VALUE" ] \
  || die "CREDENTIAL_ENCRYPTION_KEY must be independent from JWT_SECRET"
: "${PG_DB:=atcraft_erp}"
: "${PG_USER:=atcraft}"

case "$PROJECT" in
  *[!a-zA-Z0-9_-]*|"") die "COMPOSE_PROJECT_NAME may contain only letters, numbers, '_' and '-'" ;;
esac

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
  if [ "$CREDENTIAL_KEY_VALUE" = "$(other_value CREDENTIAL_ENCRYPTION_KEY)" ]; then
    die "both deployments share CREDENTIAL_ENCRYPTION_KEY; generate an independent value for one deployment"
  fi
  if [ "$(env_value POSTGRES_PASSWORD)" = "$(other_value POSTGRES_PASSWORD)" ]; then
    warn "both deployments share POSTGRES_PASSWORD."
    warn "Neither can reach the other's network, so this is not exploitable today -"
    warn "but it stops being true the moment a port is published for debugging."
  fi
fi

# Serialize every operation which can change running services, images or data.
# The lock lives in an ignored, user-owned repository directory so deployment
# does not depend on write access to /var/lock.
acquire_operation_lock() {
  command -v flock >/dev/null 2>&1 || die "flock is required for safe deployment operations"
  mkdir -p "$REPO_ROOT/deploy/.locks"
  chmod 700 "$REPO_ROOT/deploy/.locks"
  exec 9>"$REPO_ROOT/deploy/.locks/${PROJECT}.lock"
  flock -n 9 || die "another operation is already running for deployment '$DEPLOYMENT'"
}

case "$COMMAND" in
  build|up|down|destroy|restart|migrate|downgrade|seed|demo-data|psql|shell|backup|restore|backup-verify)
    acquire_operation_lock
    ;;
esac

dc() {
  # -p on every call, explicitly. The env file also sets COMPOSE_PROJECT_NAME,
  # and this is the belt to that braces: the project name is visible in every
  # command that runs, so there is no way to read the log afterwards and be
  # unsure which deployment it acted on.
  SOURCE_REVISION="$SOURCE_REVISION" IMAGE_TAG="$RELEASE_TAG" \
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
  [ -n "$current" ] || die "deployment commands must run from a Git checkout"
  [ "$current" != "HEAD" ] || die "refusing to deploy a detached HEAD; check out $DEPLOY_BRANCH"
  if [ "$current" != "$DEPLOY_BRANCH" ]; then
    die "deployment '$DEPLOYMENT' tracks branch '$DEPLOY_BRANCH' but the working tree is on '$current'.
       Run:  git checkout $DEPLOY_BRANCH
       Or, if this is deliberate:  DEPLOY_BRANCH_OVERRIDE=1 $0 $DEPLOYMENT $COMMAND"
  fi
}

assert_clean_release_source() {
  assert_branch
  [ "$SOURCE_REVISION" != "unknown" ] || die "cannot identify the source revision"
  if ! git diff --quiet -- || ! git diff --cached --quiet -- \
      || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    git status --short >&2
    die "the working tree is not clean; commit or remove every change before deploying.
       Images are tagged by commit, so shipping uncommitted bytes would make
       that tag a false provenance claim."
  fi
  if [ "${DEPLOY_REVISION_OVERRIDE:-0}" != "1" ]; then
    local upstream upstream_revision
    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    [ -n "$upstream" ] || die "branch '$DEPLOY_BRANCH' has no upstream; push the tested revision before deploying"
    upstream_revision="$(git rev-parse --verify '@{u}')"
    [ "$SOURCE_REVISION" = "$upstream_revision" ] || die "HEAD is not the tested upstream revision ($upstream).
       Push the commit and let CI pass before deployment, or set
       DEPLOY_REVISION_OVERRIDE=1 only for a documented emergency release."
  else
    warn "DEPLOY_REVISION_OVERRIDE=1 - not requiring HEAD to match its upstream"
  fi
}

assert_deployment_config() {
  local app_env cors hosts email_provider public_web_url email_from smtp_host
  local smtp_username smtp_password smtp_security mode
  app_env="$(env_value APP_ENV)"
  cors="$(env_value CORS_ORIGINS)"
  hosts="$(env_value ALLOWED_HOSTS)"
  email_provider="$(env_value EMAIL_PROVIDER)"
  public_web_url="$(env_value PUBLIC_WEB_URL)"
  email_from="$(env_value EMAIL_FROM_ADDRESS)"
  smtp_host="$(env_value SMTP_HOST)"
  smtp_username="$(env_value SMTP_USERNAME)"
  smtp_password="$(env_value SMTP_PASSWORD)"
  smtp_security="$(env_value SMTP_SECURITY)"

  case "$app_env" in
    production|staging) ;;
    *) die "APP_ENV must be production or staging in $ENV_FILE (found '${app_env:-empty}')" ;;
  esac
  [ -n "$cors" ] || die "CORS_ORIGINS is empty in $ENV_FILE"
  [ -n "$hosts" ] || die "ALLOWED_HOSTS is empty in $ENV_FILE"
  [ "$email_provider" = "smtp" ] || die "EMAIL_PROVIDER must be smtp in $ENV_FILE"
  [ -n "$email_from" ] || die "EMAIL_FROM_ADDRESS is empty in $ENV_FILE"
  [ -n "$smtp_host" ] || die "SMTP_HOST is empty in $ENV_FILE"
  case "$smtp_security" in
    starttls|tls) ;;
    *) die "SMTP_SECURITY must be starttls or tls in a deployment" ;;
  esac
  case "$public_web_url" in
    https://*) ;;
    *) die "PUBLIC_WEB_URL must be an HTTPS URL in $ENV_FILE" ;;
  esac
  if { [ -n "$smtp_username" ] && [ -z "$smtp_password" ]; } \
      || { [ -z "$smtp_username" ] && [ -n "$smtp_password" ]; }; then
    die "SMTP_USERNAME and SMTP_PASSWORD must either both be set or both be empty"
  fi
  # One variable at a time, so the refusal names the key that still holds the
  # placeholder. Tested as one concatenated string, the message could only say
  # "the file", and an operator who had set CORS_ORIGINS and ALLOWED_HOSTS - the
  # two `init` used to mention - was sent back to those rather than to SMTP_HOST.
  local kv
  for kv in CORS_ORIGINS="$cors" ALLOWED_HOSTS="$hosts" PUBLIC_WEB_URL="$public_web_url" \
            EMAIL_FROM_ADDRESS="$email_from" SMTP_HOST="$smtp_host"; do
    case "${kv#*=}" in
      *example.com*) die "${kv%%=*} in $ENV_FILE still holds the example.com placeholder; set the real value" ;;
    esac
  done
  case "$cors" in
    *localhost*|*127.0.0.1*) die "CORS_ORIGINS in a deployment must not trust localhost" ;;
  esac
  case "$hosts" in
    *'*'*|*'/'*|*':'*|*' '*|*$'\t'*)
      die "ALLOWED_HOSTS accepts comma-separated hostnames only (no wildcard, scheme, path, port or whitespace)"
      ;;
  esac
  printf '%s' "$hosts" | grep -Eq '(^|,)[[:space:]]*(,|$)' \
    && die "ALLOWED_HOSTS contains an empty hostname"

  # Secret-bearing deployment files must not be readable by another Unix user.
  # GNU stat is used on the deployment host; filesystems without POSIX modes
  # (for example a Windows checkout) are left to the explicit runbook check.
  if mode="$(stat -c '%a' "$ENV_FILE" 2>/dev/null)"; then
    case "$mode" in
      600|400) ;;
      *) die "$ENV_FILE has mode $mode; run: chmod 600 $ENV_FILE" ;;
    esac
  fi
}

LAST_BACKUP=""
create_backup() {
  local dest stamp file partial
  dest="backups/${DEPLOYMENT}"
  mkdir -p "$dest"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  file="${dest}/${PROJECT}-${PG_DB}-${stamp}.dump"
  partial="${file}.partial.$$"
  [ ! -e "$file" ] || die "refusing to overwrite existing backup $file; wait one second and retry"
  say "Dumping deployment '$DEPLOYMENT' -> $file"
  if ! dc exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" \
      -Fc --no-owner --no-privileges > "$partial"; then
    rm -f -- "$partial"
    die "pg_dump failed; no backup was published"
  fi
  if [ ! -s "$partial" ] \
      || ! dc exec -T postgres pg_restore --list < "$partial" >/dev/null; then
    rm -f -- "$partial"
    die "pg_dump did not produce a valid custom-format archive"
  fi
  chmod 400 "$partial"
  mv -- "$partial" "$file"
  chmod 400 "$file"
  LAST_BACKUP="$file"
  ok "wrote $file ($(du -h "$file" | cut -f1))"
}

postgres_container_exists() {
  local containers
  containers="$(docker ps -a \
    --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=postgres" \
    --format '{{.ID}}')" || die "cannot inspect Docker containers"
  [ -n "$containers" ]
}

postgres_volume_exists() {
  local volumes
  volumes="$(docker volume ls --filter "name=^${PROJECT}_postgres-data$" --format '{{.Name}}')" \
    || die "cannot inspect Docker volumes"
  [ "$volumes" = "${PROJECT}_postgres-data" ]
}

wait_for_postgres() {
  local attempt
  for attempt in $(seq 1 20); do
    if dc exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  die "PostgreSQL did not become ready within 40 seconds"
}

# Return success only when this project already owns database state. A stopped
# container and a preserved volume are both existing deployments, not an
# initial install, and therefore require a pre-change backup.
start_existing_postgres() {
  if postgres_container_exists; then
    dc start postgres >/dev/null
    wait_for_postgres
    return 0
  fi
  if postgres_volume_exists; then
    # `down` removes the container but deliberately preserves this volume. Use
    # the already-cached pinned image; pulling a new stateful image happens only
    # after the old data has been backed up.
    dc up -d --no-build --pull never postgres >/dev/null
    wait_for_postgres
    return 0
  fi
  return 1
}

running_application_services() {
  local services
  services="$(docker ps \
    --filter "label=com.docker.compose.project=${PROJECT}" \
    --format '{{.Label "com.docker.compose.service"}}')" || die "cannot verify application container state"
  printf '%s\n' "$services" \
    | grep -E '^(nginx|frontend|backend|celery-worker|celery-notify|celery-beat)$' || true
}

assert_application_stopped() {
  local running
  running="$(running_application_services)"
  [ -z "$running" ] || die "application services are still running and may write during a schema operation:
$running"
}

stop_application_traffic() {
  say "Stopping application traffic and writers before the schema changes"
  dc stop -t 30 nginx frontend backend celery-worker celery-notify celery-beat
  assert_application_stopped
}

start_application_traffic() {
  # `--wait` is not a traffic gate if Nginx is part of the same invocation.
  # Prove the entire application tier healthy first, then expose it at the edge.
  if ! dc up -d --no-build --wait backend frontend celery-worker celery-notify celery-beat; then
    dc stop nginx >/dev/null 2>&1 || true
    die "the application tier did not become healthy; Nginx remains stopped"
  fi
  dc up -d --no-build --wait nginx
}

release_up() {
  assert_clean_release_source
  assert_deployment_config
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable"

  say "Building immutable release $RELEASE_TAG for '$DEPLOYMENT'"
  # Build while the old release is still serving.  A compiler or test-stage
  # failure therefore causes no outage. Digests make this build reproducible;
  # dependency automation proposes reviewed security digest updates.
  dc build --pull

  stop_application_traffic

  if start_existing_postgres; then
    create_backup
  else
    info "No PostgreSQL container or data volume exists; this is an initial deployment."
  fi

  # Patch-level stateful-service updates happen only after the backup and while
  # writers are stopped.  PostgreSQL major upgrades remain a separate, explicit
  # operation; the Compose tag is pinned to major 18.
  dc pull postgres redis nginx
  dc up -d --wait postgres redis

  say "Migrating before any new application container receives traffic"
  if ! dc run --rm --no-deps -T backend alembic upgrade head; then
    warn "Migration failed. Application containers remain stopped."
    [ -n "$LAST_BACKUP" ] && warn "Pre-release backup: $LAST_BACKUP"
    die "inspect the migration and database before starting either release"
  fi

  start_application_traffic
  ok "release $RELEASE_TAG is up, migrated, and ready"
}

# --- Commands ----------------------------------------------------------------
case "$COMMAND" in

  doctor)
    assert_deployment_config
    say "Deployment '$DEPLOYMENT'"
    printf '  %-22s %s\n' "project"      "$PROJECT"
    printf '  %-22s %s\n' "env file"     "$ENV_FILE"
    printf '  %-22s %s\n' "branch"       "${DEPLOY_BRANCH:-(unpinned)}"
    printf '  %-22s %s\n' "http"         "127.0.0.1:${HTTP_PORT}"
    printf '  %-22s %s\n' "database"     "$PG_DB (as $PG_USER)"
    printf '  %-22s %s\n' "app env"      "$(env_value APP_ENV)"
    printf '  %-22s %s\n' "cors origins" "$(env_value CORS_ORIGINS)"
    printf '  %-22s %s\n' "jwt audience" "$(env_value JWT_AUDIENCE)"
    printf '  %-22s %s\n' "allowed hosts" "$(env_value ALLOWED_HOSTS)"
    printf '  %-22s %s\n' "source revision" "$SOURCE_REVISION"
    printf '  %-22s %s\n' "image tag" "$RELEASE_TAG"
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

  build)   assert_clean_release_source; assert_deployment_config; dc build --pull "$@" ;;

  up)
    [ "$#" -eq 0 ] || die "'up' accepts no extra Compose arguments; a release must run the complete guarded workflow"
    release_up
    ;;

  down)
    # No -v. Ever, from this subcommand: `down -v` on the wrong deployment is
    # unrecoverable, and the two commands differ by two characters.
    [ "$#" -eq 0 ] || die "'down' accepts no extra arguments; use the separately confirmed 'destroy' command to delete volumes"
    say "Stopping deployment '$DEPLOYMENT' - volumes and data are kept"
    dc down
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

  restart)
    [ "$#" -eq 0 ] || die "'restart' accepts no extra arguments"
    release_up
    ;;
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
    [ "${ALLOW_SCHEMA_RECOVERY:-0}" = "1" ] || die "standalone migration is recovery-only.
       Normal releases must use: ./scripts/deploy.sh $DEPLOYMENT up
       For an operator-supervised recovery, stop all application services and set ALLOW_SCHEMA_RECOVERY=1."
    assert_clean_release_source
    assert_deployment_config
    assert_application_stopped
    if start_existing_postgres; then
      create_backup
    else
      die "no existing database was found; use the complete 'up' workflow for an initial deployment"
    fi
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
    [ "$#" -le 1 ] || die "usage: ./scripts/deploy.sh $DEPLOYMENT downgrade [positive-revision-count]"
    [[ "$steps" =~ ^[1-9][0-9]*$ ]] \
      || die "downgrade count must be a positive integer"
    [ "${ALLOW_SCHEMA_RECOVERY:-0}" = "1" ] || die "downgrade is recovery-only; set ALLOW_SCHEMA_RECOVERY=1 after stopping the application tier"
    assert_clean_release_source
    assert_deployment_config
    assert_application_stopped
    start_existing_postgres || die "no existing database was found"
    warn "Rolling back $steps revision(s) on deployment '$DEPLOYMENT'."
    warn "A downgrade that drops a column destroys the data in it."
    confirm "A verified backup will be taken before rollback." "downgrade $DEPLOYMENT"
    create_backup
    dc run --rm --no-deps -T backend alembic downgrade "-${steps}"
    warn "Schema downgraded. Application services remain stopped for operator verification."
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
    # With no arguments, an interactive shell. With arguments, run exactly that
    # command in the backend container - which is how a one-off maintenance or
    # data check is run against a deployment without publishing a port or
    # copying its credentials to the host.
    if [ "$#" -eq 0 ]; then
      dc exec backend bash
    else
      dc exec backend "$@"
    fi
    ;;

  # --- Backup --------------------------------------------------------------
  backup)
    # -Fc (custom format) rather than plain SQL: it restores selectively, in
    # parallel, and compresses. pg_dump runs inside the container, so it needs
    # no published port and no host-side password.
    create_backup
    warn "This is one copy, on the machine it came from. That is not a backup"
    warn "yet (section 45) - send it off the box:"
    info "  scp $LAST_BACKUP <somewhere-else>:/backups/"
    ;;

  restore)
    file="${1:-}"
    [ "$#" -eq 1 ] || die "usage: ./scripts/deploy.sh $DEPLOYMENT restore <file.dump>"
    [ -f "$file" ] || die "no such file: $file"
    assert_clean_release_source
    assert_deployment_config
    docker info >/dev/null 2>&1 || die "Docker daemon is unavailable"
    # A restore may be the first operation after checking out a newer release.
    # Build before the maintenance window so the migration and every service
    # restarted below use the exact source revision we just verified.
    say "Building immutable release $RELEASE_TAG for '$DEPLOYMENT'"
    dc build --pull
    warn "Restoring into deployment '$DEPLOYMENT' database '$PG_DB'."
    warn "Every row currently in it that the dump also contains is replaced."
    confirm "Source: $file" "restore into $DEPLOYMENT"
    stop_application_traffic
    if ! start_existing_postgres; then
      dc up -d --no-build --wait postgres
    fi
    dc exec -T postgres pg_restore --list < "$file" >/dev/null \
      || die "the supplied file is not a valid pg_restore archive; application services remain stopped"
    create_backup
    info "Pre-restore backup: $LAST_BACKUP"
    say "Restoring..."
    # --clean --if-exists drops objects before recreating them, so a restore
    # over a populated database is deterministic rather than a pile of
    # duplicate-key errors. --single-transaction makes it all-or-nothing: a
    # restore that fails half way leaves the database as it was.
    dc exec -T postgres pg_restore -U "$PG_USER" -d "$PG_DB" \
        --clean --if-exists --no-owner --no-privileges --single-transaction < "$file"
    say "Migrating the restored database to the release schema"
    if ! dc run --rm --no-deps -T backend alembic upgrade head; then
      die "restore succeeded but migration failed; application services remain stopped"
    fi
    ok "restored and migrated"
    start_application_traffic
    ok "application tier is healthy and Nginx traffic has resumed"
    ;;

  backup-verify)
    # A backup nobody has restored is a hypothesis. This restores into a
    # scratch database inside the same cluster and compares row counts, so the
    # claim "we have backups" is testable without touching the live database.
    start_existing_postgres || die "no existing database was found"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    scratch="atcraft_verify_${DEPLOYMENT}_$$_${RANDOM}"
    tmp="backups/${DEPLOYMENT}/verify-${stamp}-$$.dump.partial"
    mkdir -p "backups/${DEPLOYMENT}"
    scratch_created=0
    cleanup_backup_verify() {
      if [ "$scratch_created" -eq 1 ]; then
        dc exec -T postgres psql -U "$PG_USER" -d postgres \
          -c "DROP DATABASE IF EXISTS \"$scratch\";" >/dev/null 2>&1 \
          || warn "could not remove scratch database $scratch"
        scratch_created=0
      fi
      rm -f -- "$tmp"
    }
    trap cleanup_backup_verify EXIT
    trap 'exit 130' INT TERM
    say "1/4 dumping $PG_DB"
    if ! dc exec -T postgres pg_dump -U "$PG_USER" -d "$PG_DB" \
        -Fc --no-owner --no-privileges > "$tmp"; then
      die "pg_dump failed during restore verification"
    fi
    [ -s "$tmp" ] || die "pg_dump produced an empty file"
    dc exec -T postgres pg_restore --list < "$tmp" >/dev/null \
      || die "pg_dump did not produce a valid custom-format archive"
    say "2/4 creating scratch database $scratch"
    dc exec -T postgres psql -U "$PG_USER" -d postgres -c "CREATE DATABASE \"$scratch\";" >/dev/null
    scratch_created=1
    say "3/4 restoring into $scratch"
    dc exec -T postgres pg_restore -U "$PG_USER" -d "$scratch" --no-owner --no-privileges < "$tmp" >/dev/null
    say "4/4 comparing"
    counts_sql="SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
    live="$(dc exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "$counts_sql" | tr -d '\r')"
    copy="$(dc exec -T postgres psql -U "$PG_USER" -d "$scratch" -tAc "$counts_sql" | tr -d '\r')"
    printf '  tables: live=%s restored=%s\n' "$live" "$copy"
    # A comparison that cannot fail is worse than none. A table psql cannot
    # count - absent, or lost by the restore - used to yield "-" on both sides,
    # which compared equal and printed as a match under a green verdict. An
    # uncountable table and a mismatch are both failures, and the verdict at
    # the end is only "verified" when nothing above disagreed.
    rc=0
    [ -n "$live" ] && [ "$live" = "$copy" ] || { warn "  table count MISMATCH live=${live:--} restored=${copy:--}"; rc=1; }
    for t in companies users items inventory_transactions audit_logs; do
      l="$(dc exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "SELECT count(*) FROM \"$t\";" 2>/dev/null | tr -d '\r' || true)"
      c="$(dc exec -T postgres psql -U "$PG_USER" -d "$scratch" -tAc "SELECT count(*) FROM \"$t\";" 2>/dev/null | tr -d '\r' || true)"
      if [ -z "$l" ] || [ -z "$c" ]; then warn "  $t could not be counted (live=${l:--} restored=${c:--})"; rc=1
      elif [ "$l" = "$c" ]; then printf '  %-18s %s == %s\n' "$t" "$l" "$c"
      else warn "  $t MISMATCH live=$l restored=$c"; rc=1; fi
    done
    # Clean up before the verdict. The EXIT trap provides the same guarantee on
    # every earlier error path, including a failed restore or interrupted run.
    cleanup_backup_verify
    trap - EXIT INT TERM
    [ "$rc" -eq 0 ] || die "restore verification FAILED - see the lines above; do not trust this backup"
    ok "restore verified against a real restore, scratch database dropped"
    ;;

  config) dc config "$@" ;;

  -h|--help|help) usage ;;
  *) die "unknown command '$COMMAND' - run './scripts/deploy.sh $DEPLOYMENT help'" ;;
esac
