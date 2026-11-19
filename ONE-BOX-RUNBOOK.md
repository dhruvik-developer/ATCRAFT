# ONE-BOX RUNBOOK

The one hosted deployment of the Atcraft ERP — production, released from `main`
— on a single host, plus the local development setup. There is no hosted
development, staging or preview deployment: development is local, on `develop`.
Commands describe the checked-in scripts and configuration; host setup and
external-provider checks still need to be run on the target installation.
Destructive operations are identified before their commands.

If you read one thing, read [What keeps the deployment
safe](#what-keeps-the-deployment-safe). The failure it exists to prevent is
silent — a command that reaches the wrong Compose project comes up perfectly
healthy — so the entry point is one script that always names the project.

---

## Contents

1. [Architecture](#architecture)
2. [What keeps the deployment safe](#what-keeps-the-deployment-safe)
3. [Local development](#local-development)
4. [Production setup, from a bare host](#production-setup-from-a-bare-host)
5. [Environment variables](#environment-variables)
6. [Everyday operations](#everyday-operations)
7. [Deploying a change](#deploying-a-change)
8. [Migrations](#migrations)
9. [Backup and restore](#backup-and-restore)
10. [Troubleshooting](#troubleshooting)
11. [Handover checklist](#handover-checklist)

---

## Architecture

```
                          the internet
                                │
                         :80  :443
                                │
                  ┌─────────────▼──────────────┐
                  │   HOST NGINX (systemd)     │   TLS terminates here, once.
                  │   /etc/nginx/conf.d/       │   Certificates renew here, once.
                  │     atcraft-main.conf      │
                  └─────────────┬──────────────┘
                erp.example.com │
                                │
                       127.0.0.1:8081           loopback only —
                                │               nothing off-box can
   ═════════════════════════════▼════════════    reach it directly
    PRODUCTION  "main"
    project: atcraft-main
    branch:  main
   ───────────────────────────────────────
    nginx      (app router)
    frontend   (static SPA)
    backend    (gunicorn x4)
    celery-worker
    celery-notify
    celery-beat
    postgres  ── volume
    redis     ── volume
              ── storage volume
   ═══════════════════════════════════════
    network: atcraft-main_internal
```

Everything that is specific to the deployment lives in `deploy/main.env`.

Only `main` and `develop` are retained as Git branches. Work on `develop` and
synchronize both to the same verified revision. The deployment is named `main`,
tracks `main` in its environment template and branch guard, and is the only
hosted deployment; `scripts/deploy.sh` refuses any other deployment name. No
separate `dev` or feature branch is required.

|                  | Production                                    |
| ---------------- | --------------------------------------------- |
| name             | `main`                                        |
| compose project  | `atcraft-main`                                |
| git branch       | `main`                                        |
| loopback port    | `8081`                                        |
| domain           | `erp.example.com`                             |
| `APP_ENV`        | `production`                                  |
| database         | `atcraft_erp` in `atcraft-main_postgres-data` |
| Redis            | `atcraft-main_redis-data`                     |
| uploads          | `atcraft-main_storage-data`                   |
| gunicorn workers | 4                                             |
| Postgres RAM     | `shared_buffers=1GB`, limit 4G                |

### Why there is an nginx in the deployment _and_ one on the host

They do different jobs, and putting both jobs in one place breaks something:

- **Host nginx** owns TLS, the public hostnames, HSTS and the HTTP→HTTPS
  redirect. The host owns `:443`, and a certificate should be
  renewed in exactly one place.
- **Deployment nginx** owns `/api` routing, per-endpoint rate limits, the
  response security headers, the SPA fallback and asset caching. Keeping it
  means the deployment is complete and testable on its own — `curl
http://127.0.0.1:8081/healthz` answers before the host nginx is configured at
  all — and the deployment behaves the same behind the host nginx as it does on
  its own.

---

## What keeps the deployment safe

The boundary is the **Compose project name**, and everything else follows from
it. Compose derives container names, the bridge network and the volume names
from the project, so `atcraft-main` gets:

```
atcraft-main_internal        the bridge network
atcraft-main_postgres-data   volumes
atcraft-main_redis-data
atcraft-main_storage-data
```

Inside the deployment, the hostname `postgres` resolves to its cluster and to
nothing else — DNS for a project exists only on that project's network.

Three things enforce it, and each replaces a failure that produces a _working_
system:

1. **`docker-compose.yml` requires `COMPOSE_PROJECT_NAME`.** A bare
   `docker compose up` in this directory fails outright. Without that, Compose
   derives the project from the directory name, and a command can land on a
   project that is not production's own while reporting every container
   healthy.
2. **No `container_name:` anywhere.** A fixed container name is claimed
   host-wide, so a second stack on the same box could not start at all.
3. **`scripts/deploy.sh` is the only entry point.** It reads
   `deploy/main.env`, takes the project name from it, passes it explicitly
   with `-p` on every command, and refuses to run on the wrong branch, from a
   dirty tree, or with placeholder or reused secrets.

---

## Local development

The local stack is separate from the hosted deployment. It is the
only one that publishes a database port, because the host-run API, the host-run
Vite server and pytest all need to reach Postgres.

Its compose project is `atcraft-erp`, set by the Makefile and `make.ps1` —
deliberately the historical name, so the volumes an existing checkout already
has, and the demo data in them, stay where they are.

Its containers are `atcraft-erp-postgres-1` and `atcraft-erp-redis-1`, publishing
`127.0.0.1:${POSTGRES_PORT}` and `127.0.0.1:${REDIS_PORT}` — 5432 and 6379
unless `.env` overrides them, which the First-time section below recommends
(5433/6380) because a host Postgres often already holds 5432. `.\make.ps1 dev`
prints the resolved ports.

### First time

```bash
git clone <repo> atcraft && cd atcraft
cp .env.example .env
```

Then edit `.env` and set, at minimum:

```ini
POSTGRES_PASSWORD=<a unique local password; URL-encode it in the DSNs below>
JWT_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(64))">
CREDENTIAL_ENCRYPTION_KEY=<generate independently with the same command>
POSTGRES_PORT=5433          # 5432 is often already taken by a host Postgres
REDIS_PORT=6380
DATABASE_URL=postgresql+asyncpg://atcraft:<encoded-password>@127.0.0.1:5433/atcraft_erp
DATABASE_SYNC_URL=postgresql+psycopg://atcraft:<encoded-password>@127.0.0.1:5433/atcraft_erp
REDIS_URL=redis://127.0.0.1:6380/0
CELERY_BROKER_URL=redis://127.0.0.1:6380/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6380/2
```

Install dependencies:

`pre-commit` must already be on your PATH when `install` registers the Git
hooks. Install it in your tooling environment with
`python -m pip install pre-commit`; see the [setup prerequisites](README.md#prerequisites).

```bash
# Linux / macOS
make install
source apps/api/.venv/bin/activate

# Windows
.\make.ps1 install
```

Activate that virtual environment in every macOS/Linux terminal that runs
Python-backed Make targets. The PowerShell wrapper selects the virtual
environment's interpreter automatically.

### Hybrid mode — the daily driver

Postgres and Redis in Docker; API and web on the host with hot reload.

```bash
make dev                 # Linux / macOS
.\make.ps1 dev           # Windows
```

On Linux and macOS that brings up infrastructure and then runs both dev
servers. On Windows it brings up infrastructure only and prints the next
commands: run `.\make.ps1 dev-api` and `.\make.ps1 dev-web` in two more
terminals. First time only, in another terminal:

```bash
make seed-local          # migrations, baseline, demonstration data and seeded PDFs
# Windows: .\make.ps1 seed-local
```

- API: <http://localhost:8000>, docs at `/docs`
- Web: <http://localhost:5173>

The seed displays the generated password for a newly created administrator
(`admin@atcraft.example` by default); change it on first login. Repeated runs
preserve existing passwords. See [Local testing seed data](docs/local-seed-data.md)
for account overrides, dataset coverage and recovery of interrupted runs.
For new background reports or MRP jobs while testing, start `make dev-worker`
(`.\make.ps1 dev-worker` on Windows); the notification queue has its own Docker
worker in the full stack.

### Full-Docker development

Everything in containers, hot reload through the stack's nginx on
<http://localhost>:

```bash
make dev-docker
```

### Local demonstration data

`atcraft_erp` is the normal local database. `seed-local` targets the matching
loopback database configured by the two DSNs and accepts only `development` or
`test` mode; it never resets the database. `atcraft_erp_test` is the separate
test database used by pytest, not a persistent UI dataset.

```bash
make db-fresh       # DESTROYS the local database, rebuilds it with demo data
make db-handover    # DESTROYS it, rebuilds with the BASELINE ONLY - no demo rows
```

`db-fresh` rebuilds rather than deletes because the demo rows span many tables
joined by foreign keys and only the parents carry a recognisable `DM-` code —
there is no reliable way to delete "just the demo data" afterwards. Run
`db-handover` before giving the client a database.

Use `seed-local` for the complete current fixture, including generated invoice
and credit-note PDFs and stock summaries. `db-fresh` remains a destructive
baseline/demo reset helper. The [seed reference](docs/local-seed-data.md) records
the current coverage and repeated-run verification; record counts change as
supported workflows grow. Read the final report for unavailable scenarios and
unexpected skips.

### Tests

```bash
make test           # backend unit + integration + api
make test-web       # frontend unit tests
make check          # contracts, lint/type checks, backend coverage, UI tests, build
make clean          # generated outputs only; retains configuration, data, dependencies
```

PowerShell equivalents are `.\make.ps1 <target>`.
`make check` is the shared application gate. CI additionally runs dependency
and security audits, schema checks, browser coverage and deployment checks; see
[Development standards](docs/development-standards.md). Preview cleanup with
`python scripts/clean.py --dry-run` before removing local generated artifacts.

---

## Production setup, from a bare host

Assumes Ubuntu 22.04/24.04 with a sudo user. Substitute your real hostname for
`erp.example.com` throughout.

### 1. DNS

Point the name at the box before requesting the certificate:

```
erp.example.com.       A   <host ip>
```

### 2. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version          # must be v2.20 or newer
```

### 3. Firewall — only 80 and 443

The deployment publishes on loopback only, so this is defence in depth rather
than the only thing standing between the internet and Postgres.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

### 4. Checkout and branch

The deployment builds from a working tree on `main`.

```bash
sudo mkdir -p /srv && sudo chown "$USER" /srv

git clone <repo> /srv/atcraft-main
cd /srv/atcraft-main && git checkout main
```

`scripts/deploy.sh` refuses to build from a tree that is not on `main`, so a
forgotten `git checkout` cannot ship the wrong code. These server provisioning
examples do not authorize a second checkout on the local development machine.

### 5. Generate the deployment's secrets

```bash
cd /srv/atcraft-main && ./scripts/deploy.sh main init
```

This writes `deploy/main.env`, mode 600, with a freshly generated
`POSTGRES_PASSWORD`, `JWT_SECRET`, and `CREDENTIAL_ENCRYPTION_KEY`. No value is
printed or logged. The encryption key is independent so JWT rotation does not
invalidate enrolled authenticators. `init` refuses to overwrite an existing file.

Then edit the file and replace every `example.com` placeholder. Five keys
ship with one — the two public-hostname keys and three mail keys — and `doctor`
and `up` refuse the file while any of them still holds it, naming the key. The
values below are examples only:

```ini
# /srv/atcraft-main/deploy/main.env
CORS_ORIGINS=https://erp.yourcompany.com
ALLOWED_HOSTS=erp.yourcompany.com
PUBLIC_WEB_URL=https://erp.yourcompany.com
EMAIL_FROM_ADDRESS=no-reply@yourcompany.com
SMTP_HOST=smtp.yourcompany.com
SMTP_USERNAME=<relay account, if the relay authenticates>
SMTP_PASSWORD=<its password - both set, or both empty>
```

Mail is not optional in a deployment: a password-reset response must
correspond to a deliverable link, so `doctor` also requires
`EMAIL_PROVIDER=smtp`, `SMTP_SECURITY=starttls` or `tls`, an `https://`
`PUBLIC_WEB_URL`, and `SMTP_USERNAME`/`SMTP_PASSWORD` either both set or both
empty. The templates already carry the right `EMAIL_PROVIDER`, `SMTP_PORT` and
`SMTP_SECURITY`.

### Optional WhatsApp and SMS

Keep both channels disabled until the customer's legal basis, provider account
and approved templates are ready. Before enabling:

1. Complete Meta Business verification and register the WhatsApp Business phone
   number. Submit and obtain approval for each WhatsApp message template.
2. Register the SMS sender and message templates with the Indian DLT operator,
   and obtain the provider auth key, entity ID and approved verification-code
   template. Confirm the provider's data-processing terms and consent process.
3. Add the provider settings to the deployment env file. Keep credentials only
   in this file or the deployment secret manager; never put them in the database
   or source control. The provider host must be included in
   `MESSAGING_ALLOWED_HOSTS`.
4. Start with `SMS_ENABLED=false` and `WHATSAPP_ENABLED=false`; run
   `./scripts/deploy.sh main doctor`, then deploy. Afterward verify an
   operator's number with the SMS OTP from their profile, record their explicit
   channel consent, and configure each tenant's templates at
   **Administration → Messaging templates**.
5. Test only to the signed-in administrator's own verified, opted-in phone.
   Review the delivery log and provider account before enabling notifications
   for users. WhatsApp activation also requires the SMS OTP settings because
   phone verification is SMS-based.

Example (values are placeholders; the secret fields must be supplied securely):

```ini
WHATSAPP_ENABLED=false
WHATSAPP_API_BASE=https://graph.facebook.com
WHATSAPP_API_VERSION=v20.0
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_ACCESS_TOKEN=
SMS_ENABLED=false
SMS_PROVIDER=msg91
SMS_API_URL=https://control.msg91.com/api/v5/flow/
SMS_AUTH_KEY=
SMS_SENDER_ID=
SMS_DLT_ENTITY_ID=
SMS_VERIFICATION_TEMPLATE_ID=
MESSAGING_ALLOWED_HOSTS=graph.facebook.com,control.msg91.com
```

Check the result without starting anything:

```bash
./scripts/deploy.sh main doctor
```

**Never copy secrets from another system.** `deploy.sh` refuses to run if
`JWT_SECRET` or `CREDENTIAL_ENCRYPTION_KEY` is empty, short, a template
placeholder, or if the two are equal.

### 6. Start the deployment

```bash
cd /srv/atcraft-main && ./scripts/deploy.sh main up
```

`up` runs the complete guarded release: it builds commit-addressed images while
the old release is live, stops application traffic, takes a pre-migration
backup when a database already exists, updates infrastructure, migrates, then
starts the new application and waits for dependency-aware readiness.

### 7. Seed the schema

```bash
cd /srv/atcraft-main && ./scripts/deploy.sh main seed
```

Do **not** load demo data into the deployment. `deploy.sh` will demand a typed
confirmation if you try, because `APP_ENV=production` there.

### 8. Certificates

Issue the certificates _before_ the host configs go in. The `:443` server
block in `atcraft-main.conf` names
`/etc/letsencrypt/live/<hostname>/`, so on a bare host `nginx -t` fails on
that file every time until the certificates exist — not conditionally — and
a reload chained behind it never runs.

What answers the ACME challenge at this point is the packaged default site
(`server_name _`, root `/var/www/html`), which serves any hostname the moment
the package is installed. The symlink lets it serve the challenge from
`/var/www/certbot`, the directory the atcraft `:80` blocks serve later. That
matters: certbot records the webroot a certificate was issued with and renews
against it, so a certificate issued against `/var/www/html` would fail every
renewal once step 9 has replaced that site.

```bash
sudo apt-get update && sudo apt-get install -y nginx certbot
sudo mkdir -p /var/www/certbot
sudo ln -sfn /var/www/certbot/.well-known /var/www/html/.well-known

sudo certbot certonly --webroot -w /var/www/certbot -d erp.yourcompany.com \
  --agree-tos -m ops@yourcompany.com --no-eff-email
ls /etc/letsencrypt/live/      # erp.yourcompany.com
```

### 9. Host nginx

```bash
sudo mkdir -p /etc/nginx/snippets
sudo cp /srv/atcraft-main/infrastructure/nginx/host/snippets/atcraft-proxy.conf \
        /etc/nginx/snippets/
sudo cp /srv/atcraft-main/infrastructure/nginx/host/00-default.conf \
        /srv/atcraft-main/infrastructure/nginx/host/atcraft-main.conf \
        /etc/nginx/conf.d/

# Real hostnames
sudo sed -i 's/erp\.example\.com/erp.yourcompany.com/g' /etc/nginx/conf.d/atcraft-main.conf

# Load the installed copies in a throwaway nginx before touching the live one:
# a fault here takes production offline, at reload time
cd /srv/atcraft-main && ./scripts/check-host-nginx.sh --installed
```

`--installed` reads `/etc/nginx/conf.d` and `/etc/nginx/snippets`, generates
self-signed certificates at the exact paths the configs name, and runs
`nginx -t` plus the routing, port and closed-endpoint assertions. It also refuses any
installed config that still says `example.com`. Without the flag the script
checks the repository copies, which keep the placeholders — customising those
instead would dirty the working tree that `deploy.sh up` requires clean.

Remove the packaged default site. The supplied `00-default.conf` becomes the
only default and rejects unknown HTTP Host and TLS SNI values:

```bash
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo systemctl enable --now certbot.timer     # renewal
sudo certbot renew --dry-run                  # now through the :80 blocks and /var/www/certbot
```

### 10. Verify

```bash
cd /srv/atcraft-main
./scripts/deploy.sh main health

curl -sI https://erp.yourcompany.com | head -1
```

---

## Environment variables

### The deployment — `deploy/main.env`

Consumed by Docker Compose interpolation. `(UNIQUE)` marks a value that must be
this deployment's own - generated fresh or specific to it - never copied from
another system or an example.

| Variable                                        | Example                             | Notes                                                                                                                                                            |
| ----------------------------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `COMPOSE_PROJECT_NAME`                          | `atcraft-main`                      | **(UNIQUE)** The project boundary. Required.                                                                                                                     |
| `DEPLOY_BRANCH`                                 | `main`                              | Branch this deployment tracks; `deploy.sh` enforces it.                                                                                                          |
| `HTTP_PORT`                                     | `8081`                              | **(UNIQUE)** Loopback port. Must match the host nginx `proxy_pass`. Required.                                                                                    |
| `APP_ENV`                                       | `production`                        | `production`; `deploy.sh` also accepts `staging`.                                                                                                                |
| `LOG_LEVEL`                                     | `INFO`                              |                                                                                                                                                                  |
| `CORS_ORIGINS`                                  | `https://erp.example.com`           | Comma-separated. Never `*` with credentials.                                                                                                                     |
| `ALLOWED_HOSTS`                                 | `erp.example.com`                   | Hostnames only. Required; never `*`.                                                                                                                             |
| `EMAIL_PROVIDER`                                | `smtp`                              | Required; `doctor` refuses anything else. A deployment fails closed without working mail.                                                                        |
| `PUBLIC_WEB_URL`                                | `https://erp.example.com`           | The origin password-reset links point at. Must be `https://`; must not contain `example.com`.                                                                    |
| `EMAIL_FROM_ADDRESS`                            | `no-reply@example.com`              | Sender address. Required; must not contain `example.com`.                                                                                                        |
| `EMAIL_FROM_NAME`                               | `Atcraft Manufacturing ERP`         | Sender display name.                                                                                                                                             |
| `SMTP_HOST`                                     | `smtp.example.com`                  | The relay. Required; must not contain `example.com`.                                                                                                             |
| `SMTP_PORT`                                     | `587`                               |                                                                                                                                                                  |
| `SMTP_SECURITY`                                 | `starttls`                          | `starttls` or `tls`; `doctor` refuses anything else.                                                                                                             |
| `SMTP_USERNAME`                                 | _(empty)_                           | Only if the relay authenticates. Set both or neither.                                                                                                            |
| `SMTP_PASSWORD`                                 | _(empty)_                           | Paired with `SMTP_USERNAME`.                                                                                                                                     |
| `SMTP_TIMEOUT_SECONDS`                          | `15`                                |                                                                                                                                                                  |
| `MALWARE_SCANNER`                               | `clamav` in deployed Compose       | `clamav` scans every upload; `none` leaves content checks only. `deploy.sh` activates the pinned, private ClamAV service profile and waits for its image healthcheck. |
| `MALWARE_SCAN_FAILURE_POLICY`                   | `closed`                            | `closed` refuses an upload the scanner could not check; `open` accepts and logs.                                                                                 |
| `CLAMAV_HOST` / `CLAMAV_PORT`                   | `clamav` / `3310` in deployed Compose | TCP address of the daemon. It is not published on the host; API access is limited to the deployment's private network.                                        |
| `CLAMAV_SOCKET_PATH`                            | _(empty)_                           | Unix socket; takes precedence over host/port when set.                                                                                                           |
| `CLAMAV_TIMEOUT_SECONDS`                        | `10`                                | Per-scan ceiling, 1..120 seconds.                                                                                                                                |
| `MFA_REQUIRED_ROLES`                            | `SUPER_ADMIN,ADMIN,ACCOUNTS`        | Comma-separated role preset codes (`SUPER_ADMIN`, `ADMIN`, `ACCOUNTS`, ... in `core/permissions.py`) that must enrol TOTP before using the API; matching is case-insensitive. A code no role carries enforces nothing. Clear the list to leave MFA optional. |
| `WEB_PUSH_ENABLED`                              | `false`                             | Device channel; true for container-run API/workers in the local dev overlay. Host-run processes use `.env`.                                                      |
| `WEB_PUSH_VAPID_PRIVATE_KEY`                    | _(empty)_                           | Optional base64url P-256 scalar; default derives from credential root.                                                                                           |
| `WEB_PUSH_SUBJECT`                              | _(empty)_                           | mailto: or https: contact; falls back to EMAIL_FROM_ADDRESS; required deployed/enabled.                                                                          |
| `WEB_PUSH_TTL_SECONDS`                          | `86400`                             | Provider retention, 0..2419200 seconds.                                                                                                                          |
| `WEB_PUSH_TIMEOUT_SECONDS`                      | `10`                                | HTTPS transport timeout, 1..60 seconds.                                                                                                                          |
| `WEB_PUSH_ALLOWED_HOSTS`                        | Google FCM, Mozilla, Windows, Apple | Dot-boundary suffixes: fcm.googleapis.com, updates.push.services.mozilla.com, push.services.mozilla.com, notify.windows.com, web.push.apple.com, push.apple.com. |
| `ALERTS_ENABLED`                                | `true`                              | Enables the scheduled smart-alert scan. Per-company rules are managed in Administration → Smart alerts.                                                            |
| `ALERTS_SCAN_INTERVAL_SECONDS`                  | `900`                               | Celery smart-alert scan cadence; minimum 60 seconds. Rule thresholds and reminder intervals are company settings.                                                     |
| `MRP_SCHEDULE_CRON`                             | `30 20 * * *`                       | Nightly MRP beat schedule; default is 02:00 Asia/Kolkata. Each company also controls whether its schedule is enabled and its scope/horizon.                            |
| `COURIER_TRACKING_ENABLED`                      | `false`                             | Enable only after migration and provider setup; the API/webhook/polling remain disabled otherwise.                                                                  |
| `COURIER_POLL_INTERVAL_SECONDS`                 | `1800`                              | Shiprocket polling cadence; minimum 60 seconds.                                                                                                                   |
| `COURIER_ALLOWED_HOSTS`                         | `apiv2.shiprocket.in`               | HTTPS hostname allow-list for the Shiprocket client; retain only provider hosts the deployment uses.                                                              |
| `POSTGRES_DB`                                   | `atcraft_erp`                       | Not a secret; the default is fine.                                                                                                                               |
| `POSTGRES_USER`                                 | `atcraft`                           | May match.                                                                                                                                                       |
| `POSTGRES_PASSWORD`                             | —                                   | **(UNIQUE)** Required, no default.                                                                                                                               |
| `JWT_SECRET`                                    | —                                   | **(UNIQUE)** Required. A copy held anywhere else can mint tokens for production.                                                                                 |
| `CREDENTIAL_ENCRYPTION_KEY`                     | -                                   | **(UNIQUE)** Required; encrypts MFA and reset-delivery credentials and must differ from `JWT_SECRET`.                                                            |
| `JWT_ISSUER`                                    | `atcraft-erp-main`                  | **(UNIQUE)**                                                                                                                                                     |
| `JWT_AUDIENCE`                                  | `atcraft-erp-main`                  | **(UNIQUE)**                                                                                                                                                     |
| `IMAGE_PREFIX`                                  | `atcraft`                           |                                                                                                                                                                  |
| `IMAGE_TAG`                                     | _(derived)_                         | `deploy.sh` forces the exact 12-character Git revision.                                                                                                          |
| `VITE_ERROR_REPORT_URL`                         | _(empty)_                           | Empty disables browser crash reporting. Use a same-origin collector path unless its HTTPS origin is deliberately added to the CSP.                               |
| `SENTRY_DSN`                                    | _(empty)_                           | Empty disables scrubbed backend exception reporting. Use a dedicated Sentry project for production.                                                         |
| `PG_MAX_CONNECTIONS`                            | `150`                               | Reconcile all API, batch, notification and beat pools.                                                                                                           |
| `PG_SHARED_BUFFERS`                             | `1GB`                               | ~25% of this deployment's RAM allowance.                                                                                                                         |
| `PG_EFFECTIVE_CACHE_SIZE`                       | `3GB`                               | ~50–75% of it.                                                                                                                                                   |
| `PG_WORK_MEM`                                   | `8MB`                               | Per sort/hash node; not a per-connection cap.                                                                                                                    |
| `PG_MAINTENANCE_WORK_MEM`                       | `256MB`                             |                                                                                                                                                                  |
| `PG_LOG_MIN_DURATION_MS`                        | `500`                               | Slow-query threshold.                                                                                                                                            |
| `PG_MEM_LIMIT`                                  | `4G`                                | Container ceiling.                                                                                                                                               |
| `REDIS_MAXMEMORY`                               | `1gb`                               |                                                                                                                                                                  |
| `REDIS_MEM_LIMIT`                               | `1500M`                             |                                                                                                                                                                  |
| `GUNICORN_WORKERS`                              | `4`                                 |                                                                                                                                                                  |
| `BACKEND_MEM_LIMIT`                             | `2G`                                |                                                                                                                                                                  |
| `CELERY_CONCURRENCY`                            | `4`                                 |                                                                                                                                                                  |
| `WORKER_MEM_LIMIT`                              | `2G`                                |                                                                                                                                                                  |
| `CLAMAV_MEM_LIMIT`                              | `2G`                                | ClamAV sidecar; allow up to 4G.                                                                                                                                  |
| `CLAMAV_CPU_LIMIT`                              | `1`                                 | Per deployment signature update and scan ceiling.                                                                                                                |
| `NOTIFY_CONCURRENCY`                            | `2`                                 | Dedicated notification worker.                                                                                                                                   |
| `NOTIFY_DB_POOL_SIZE`, `NOTIFY_DB_MAX_OVERFLOW` | `2`, `2`                            | Per notification child.                                                                                                                                          |
| `NOTIFY_MEM_LIMIT`                              | `512M`                              | Notification worker memory limit.                                                                                                                                |

The services share one box's RAM. Size them together: `PG_MEM_LIMIT`, the
backend, the workers and the ClamAV sidecar must fit, with room for the page
cache. The ClamAV sidecar is capped at 2G by default; lower neither limit
without measuring the database and scan workload.

### Connections and memory

Two sums have to be reconciled whenever any of these numbers change, and
nothing enforces them, so check them by hand.

**Connections.** Every gunicorn worker and every Celery prefork child builds
its own SQLAlchemy pool, so the demand is per _process_, not per container:

```
  GUNICORN_WORKERS   x (DB_POOL_SIZE + DB_MAX_OVERFLOW)          4 x 20 =  80
  CELERY_CONCURRENCY x (WORKER_DB_POOL_SIZE + ...OVERFLOW)       4 x 10 =  40
  NOTIFY_CONCURRENCY x (NOTIFY_DB_POOL_SIZE + ...OVERFLOW)       2 x  4 =   8
  celery-beat        x (1 + 1)                                            2
                                                                        ---
                                                                       130   <= 150
```

Leave headroom above the total: `psql`, a backup and a monitoring probe all
need a connection, and Postgres reserves three for superusers.

**Memory.** `work_mem` is charged per sort or hash node, so the pathological
case is `max_connections x work_mem`, and that has to leave room for
`shared_buffers` inside `PG_MEM_LIMIT`:

```
  150 x 8MB + 1GB    = 2.2G   <= 4G
```

Raising `max_connections` means lowering `work_mem`, or the container reaches
its limit and the kernel kills Postgres. A report that genuinely needs more can
raise `work_mem` for its own session.

**CPU.** Every service carries a `cpus` ceiling as well as a memory one
(`*_CPU_LIMIT` in the env file). Memory was capped and CPU was not, which meant
one service could take every core and starve the others. A ceiling is not a
reservation: keep the sum at or below the core count for hard isolation, or
above it if you would rather they share and none can monopolise.

### Fixed by the compose files, not by env

Set in `docker-compose.yml` / `docker-compose.prod.yml` so they cannot drift
from the tested configuration: `DATABASE_URL`, `DATABASE_SYNC_URL`, `REDIS_URL`,
`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `STORAGE_PATH`, `APP_DEBUG=false`,
`LOG_JSON=true`, `CACHE_ENABLED=true`, `RATE_LIMIT_ENABLED=true`.

The database and Redis URLs are built from `POSTGRES_*` and point at the
service names `postgres` and `redis`, which resolve on the deployment's own
network only. There is no host-side connection string to get wrong.

### Local development — `.env` at the repository root

Read by the app directly (`pydantic-settings`) and by Compose for the local
stack. See `.env.example`, which lists every setting with its purpose. Never
commit a populated one; `.gitignore` covers `.env` and `deploy/*.env`.

### Nothing secret reaches the browser

Only `VITE_*` variables are exposed to the frontend, and Vite inlines them into
the bundle at build time — everything in `dist/` is public and cached
indefinitely. Three of them exist: `VITE_API_BASE_URL` (a relative path),
`VITE_APP_RELEASE` and `VITE_ERROR_REPORT_URL`. The template's
`VITE_API_PROXY_TARGET` and `VITE_USE_POLLING` are read only by
`vite.config.ts` for the dev server and are never inlined.

`npm run build` fails if a credential reaches the bundle:

```bash
cd apps/web && npm run check:secrets
```

It scans the built output — after minification and after every substitution
Vite performs — for JWTs, private keys, database and Redis URLs carrying
passwords, cloud access keys, service tokens, and this project's own demo
passwords. Grepping the source is not the same check; the artefact is what
ships. The gate runs inside the frontend image build, so it applies to both
deployments.

---

### Connect a courier (D3)

Courier tracking is disabled by default. Before enabling it, apply the courier
tracking Alembic migration and confirm `CREDENTIAL_ENCRYPTION_KEY` is backed up
with the database; it encrypts both provider credentials and webhook secrets.
Set `COURIER_TRACKING_ENABLED=true`, keep
`COURIER_ALLOWED_HOSTS=apiv2.shiprocket.in` unless the approved provider
requires another explicit host, and deploy the API and worker together.

In the staff UI, open **Administration → Courier integration**, create a
Shiprocket account and use **Test connection**. Use **Rotate webhook** to issue
the callback URL and one-time secret, then configure the provider's callback
header before dismissing the secret. Rotation invalidates the previous token
and secret. Generic providers use the same callback URL and sign the exact
request body and timestamp using the displayed HMAC secret.

Use the refresh action on a tracked dispatch or inbound shipment to verify one
consignment. The worker polls every `COURIER_POLL_INTERVAL_SECONDS` seconds
(1,800 by default), at most 100 oldest eligible consignments per account. Check
the account's **Last poll** and **Last poll failed** fields after deployment.
Automatic outbound delivery confirmation defaults on per account; disable it
when the business requires a staff member to confirm courier delivery scans.
Never put credentials or webhook secrets into a URL, source file or incident
log.

## Everyday operations

Every command takes the deployment name. There is no default.

```bash
./scripts/deploy.sh main ps               # container status
./scripts/deploy.sh main health           # containers + HTTP + readiness detail
./scripts/deploy.sh main logs             # follow all
./scripts/deploy.sh main logs backend     # follow one
./scripts/deploy.sh main logs celery-worker
./scripts/deploy.sh main restart          # the full guarded release again - same as up
./scripts/deploy.sh main psql             # psql in this deployment's database
./scripts/deploy.sh main shell            # shell in this deployment's backend
./scripts/deploy.sh main doctor           # configuration, without starting anything
```

### Live debugging

Deployed processes emit **one JSON object per line** on stdout when
`LOG_JSON=true` (the default in `deploy/*.env.example` and Compose). Every
request carries `request_id`; authenticated traffic also carries `company_id`
and `user_id`. Filter a failing call by copying the `request_id` from the
error envelope or the access log line, then:

```bash
./scripts/deploy.sh main logs backend | grep '"request_id":"<id>"'
./scripts/deploy.sh main logs celery-worker | grep '"request_id":"<id>"'
```

Use `LOG_LEVEL=DEBUG` only briefly while investigating, and reproduce locally
where you can; leave `main` at `INFO`. Do not set `APP_DEBUG=true` — production
startup refuses it, and stack traces must never reach the browser.

`restart` is not a bounce. It re-runs the guarded release, so it refuses a
dirty tree or a `HEAD` that is not the pushed upstream, rebuilds the images,
takes a backup and migrates — exactly what `up` does. Use it to pick up an
edited `deploy/main.env`; do not reach for it to "just restart" a box with
uncommitted changes on it, because it will refuse.

`down` keeps the data:

```bash
./scripts/deploy.sh main down             # containers removed, volumes kept
```

`destroy` does not, and asks first:

```bash
./scripts/deploy.sh main destroy          # requires typing "destroy main"
```

`deploy.sh down` never passes `-v`. The two commands differ by two characters
and one of them is unrecoverable.

### Operational endpoints

`/health/live`, `/health/ready` and `/metrics` are served on the deployment's
loopback port and return **404 through the host nginx**. Readiness names which
dependency is down; metrics publishes request rates. Both are for whoever runs
the box.

```bash
curl -s http://127.0.0.1:8081/health/ready
# {"status":"ready","checks":{"database":true,"redis":true}}
```

---

## Deploying a change

```bash
cd /srv/atcraft-main
git fetch origin
git status                                  # must be clean
git pull --ff-only origin main

./scripts/deploy.sh main doctor
./scripts/deploy.sh main up                 # backup + migrate happen before new traffic
./scripts/deploy.sh main health
```

`up` refuses a dirty tree, detached HEAD, wrong branch, insecure env-file mode,
placeholder host configuration, or a `HEAD` that is not the pushed upstream
commit. Images are tagged and labelled with the
exact commit, so the running artifact can be traced and an old commit's image
cannot be silently overwritten by a later branch build.

The host nginx under `/etc/nginx` is not part of `up`. When a release changes
`infrastructure/nginx/host/`, install it as
[below](#when-a-release-changes-the-host-nginx) - the same pull, one more step.

Verify and synchronize the two branches, then release production from `main`:

```
develop → checks → same commit on develop and main → deployment verification
```

### When a release changes the host nginx

`up` restarts the deployment's own nginx, which reads
`infrastructure/nginx/nginx.conf` and `infrastructure/nginx/conf.d/` from this
checkout, so a change there takes effect with the release. The host nginx does
not: it reads the copies [step 9](#9-host-nginx) installed under `/etc/nginx`,
and nothing but this procedure updates them. After `git pull`, compare the
installed copies with the checkout - the `sed` puts the real hostname into the
template, so only real differences show:

```bash
cd /srv/atcraft-main
sed 's/erp\.example\.com/erp.yourcompany.com/g' infrastructure/nginx/host/atcraft-main.conf \
  | diff -u /etc/nginx/conf.d/atcraft-main.conf -
diff -u /etc/nginx/conf.d/00-default.conf infrastructure/nginx/host/00-default.conf
diff -u /etc/nginx/snippets/atcraft-proxy.conf infrastructure/nginx/host/snippets/atcraft-proxy.conf
```

No output means the host is current. Any output is a change the release made -
or one made by hand on the box, which belongs in the repository before it is
overwritten. Install the release's copies the way step 9 does, load them in a
throwaway nginx, then reload the live one:

```bash
sudo cp infrastructure/nginx/host/snippets/atcraft-proxy.conf /etc/nginx/snippets/
sudo cp infrastructure/nginx/host/00-default.conf \
        infrastructure/nginx/host/atcraft-main.conf /etc/nginx/conf.d/
sudo sed -i 's/erp\.example\.com/erp.yourcompany.com/g' /etc/nginx/conf.d/atcraft-main.conf
./scripts/check-host-nginx.sh --installed
sudo nginx -t && sudo systemctl reload nginx
```

A reload does not drop traffic: nginx starts workers on the new configuration
and lets the old ones finish the requests they hold.

### Once, when upgrading past the tax-identifier release

GSTIN validation now verifies the check digit, which earlier releases did not.
Reads are unaffected - lists, reports and documents all work - but the value is
checked when a record is _saved_, and the customer and vendor forms resend the
number they loaded. A row holding a figure that an earlier release accepted
therefore cannot be saved again until it is corrected, and whoever edits it is
stopped by a field they never touched.

Find those rows before anybody meets one:

```bash
./scripts/deploy.sh main shell python -m scripts.check_tax_identifiers
```

It only reads. It prints the entity, the code and the reason for each value,
and exits non-zero if there are any, so it can gate a deploy. A clean database
prints "Every stored GSTIN and PAN is valid."

---

## Migrations

The deployment migrates its own database. The command runs inside that
project, so it can only reach that project's Postgres — there is no connection
string to point at the wrong one.

```bash
./scripts/deploy.sh main migrate-status     # current revision + heads
```

Normal releases always use `up`, which owns stop, backup, migration, readiness,
and traffic ordering. Standalone migration is recovery-only: all application
services must already be stopped and the operator must set
`ALLOW_SCHEMA_RECOVERY=1` explicitly.

Rolling back asks for confirmation, because a downgrade that drops a column
destroys the data in it:

```bash
ALLOW_SCHEMA_RECOVERY=1 ./scripts/deploy.sh main downgrade 1
```

Authoring a migration is a development task, against the local stack:

```bash
make migration m="add packing slip reference"
# review the generated file - locking impact, data migration, rollback
make migrate
make test
```

`./scripts/deploy.sh main up` is the order of operations for every release. It
builds first, then stops HTTP and workers, takes a consistent backup, updates
Postgres/Redis patch images, runs `alembic upgrade head` from the new immutable
backend image, and only then starts new application containers. If migration
fails, application traffic remains stopped and the script prints the backup
path; new code is never served against the old schema.

The standalone `migrate` command is for recovery and operator inspection. A
normal release must not split `up` and `migrate` into separate steps.

**Pre-squash databases (before 8 November 2026).** The first release onto a
database that still records a revision of the retired chain must not use the
normal `upgrade` path: on 8 November 2026 the 38-revision chain was replaced by a
single baseline, `20261127_0100`, and replaying it against populated data is
destructive. Confirm the target's `alembic_version` is exactly `20261126_0100`;
an earlier revision must first be upgraded through the old chain at tag
`pre-migration-squash-20261127` (commit `904df1b2`, an ancestor of `main`, so it
can be checked out by hash where the tag has not been fetched). The October tag
`pre-migration-squash-20261019` (commit `af1186b4`) remains available for a
database that never reached the November chain; its own procedure is
`docs/implementation/migration-squash-20261021.md`. Then verify a clone and run
`alembic stamp --purge 20261127_0100` in the maintenance window before starting
the new release. Stamping is only the bridge: the normal `up` then applies the
revisions after the baseline (currently `20261128_0100`). See
`docs/implementation/migration-squash-20261127.md` for the verification,
stamp and recovery procedure.

---

## Backup and restore

### Take a backup

```bash
./scripts/deploy.sh main backup
# → backups/main/atcraft-main-atcraft_erp-20260903T140000Z.dump   (mode 400)
```

`pg_dump -Fc` — custom format, so it restores selectively and in parallel.
`pg_dump` runs inside the container, so no host port and no host-side password.
The file is written mode 400 because a dump is a complete copy of the business,
including every password hash.

**A dump on the machine it came from is not a backup.** Send it off the box:

```bash
scp backups/main/atcraft-main-*.dump backup-host:/backups/atcraft/main/
```

### Automate it

```bash
sudo tee /etc/cron.d/atcraft-backup >/dev/null <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
30 1 * * * ubuntu cd /srv/atcraft-main && ./scripts/deploy.sh main backup >> /var/log/atcraft-backup.log 2>&1
CRON
```

Add off-box copying and retention to the same job. Nightly dumps with no
retention will fill the disk, and a full disk stops Postgres.

### Restore

```bash
./scripts/deploy.sh main restore backups/main/atcraft-main-atcraft_erp-20260903T140000Z.dump
```

Requires typing `restore into main`. Runs `--single-transaction` with
`--clean --if-exists`, so it is all-or-nothing: a restore that fails part way
leaves the database as it was, rather than half-replaced.

`restore` stops the application tier before it writes and starts it again
afterwards — the same traffic gate as `up` — so no worker holds a connection
from before the restore. Before the maintenance window it builds the immutable
application images for the checked-out revision. After the dump is restored it
runs `alembic upgrade head`, and it resumes traffic only after the migrated
application tier is healthy. A separate `restart` is not needed, and would not
be a bounce anyway: it is the full guarded release.

### Prove the backups work

```bash
./scripts/deploy.sh main backup-verify
```

Dumps the live database, creates a scratch database in the same cluster,
restores into it, compares the table count and the row counts of
`companies`, `users`, `items`, `inventory_transactions` and `audit_logs`, then
drops the scratch database. The live database is never written to. It fails,
and says which line disagreed, when any count differs or when a table cannot
be counted on either side — a table the restore lost is not a match.

A backup nobody has restored is a hypothesis. Run this monthly.

### Disaster recovery, from nothing

```bash
git clone <repo> /srv/atcraft-main && cd /srv/atcraft-main && git checkout main
cp <saved copy of deploy/main.env> deploy/main.env && chmod 600 deploy/main.env
./scripts/deploy.sh main up
./scripts/deploy.sh main restore /backups/atcraft/main/<latest>.dump
./scripts/deploy.sh main health
```

Keep `deploy/main.env` somewhere safe and off the box. Without `JWT_SECRET` the
dump still restores, but every session is invalidated; without
`POSTGRES_PASSWORD` a **restored volume** cannot be opened at all — the password
lives in the cluster, not in the env file, so a new random one will not match.

Uploaded documents live in the `storage-data` volume, not in the database
dump. Back them up too:

```bash
docker run --rm -v atcraft-main_storage-data:/data:ro -v "$PWD/backups/main:/out" \
  alpine tar czf /out/storage-$(date -u +%Y%m%dT%H%M%SZ).tar.gz -C /data .
```

Runtime storage is ignored at both `storage/**` and `apps/api/storage/**`.
Never force-add a generated PDF, CSV, upload, export, or attachment. If real
customer documents were ever pushed before this rule was added, removing them
in a later commit is not sufficient: treat the remote as disclosed, arrange a
coordinated `git filter-repo` history rewrite, and expire every old clone and
artifact. Do not rewrite shared history as an unattended deploy step.

---

## Troubleshooting

### `COMPOSE_PROJECT_NAME must be set`

You ran `docker compose` directly. Use `./scripts/deploy.sh main …`, or
for the local stack `make dev` (hybrid) or `make dev-docker` (full Docker) —
`.\make.ps1 dev` / `.\make.ps1 dev-docker` on Windows. The requirement is
deliberate — see [What keeps the deployment
safe](#what-keeps-the-deployment-safe).

### `deployment 'main' tracks branch 'main' but the working tree is on '...'`

Use `git switch main` in the existing checkout. Do not create another branch or
checkout, or bypass the guard.

### `unknown deployment '...'`

The project has one hosted deployment, production, released from `main`. Run
`./scripts/deploy.sh main <command>`. Development runs locally on `develop`
(see [Local development](#local-development)); there is no hosted development
deployment to name.

### `HEAD is not the tested upstream revision`

The checkout is ahead of, behind, or beside what was pushed — typically a
hot-fix committed on the box, or a `git fetch` without the pull. Images are
tagged by commit, and the tag is only a provenance claim if that commit is the
one CI tested. Push the commit and let CI pass, then `git pull --ff-only`.
`DEPLOY_REVISION_OVERRIDE=1 ./scripts/deploy.sh main up` exists for a
documented emergency only; a branch with no upstream at all is refused the
same way.

### `CREDENTIAL_ENCRYPTION_KEY must be independent from JWT_SECRET`

`deploy.sh` refuses to continue. Generate an independent value, but do not
rotate a credential-encryption key after MFA enrollment unless you also have a
planned data re-encryption procedure.

### The deployment will not bind its port

```bash
sudo ss -ltnp | grep ':8081'
```

Something else on the box already listens on the loopback port named by
`HTTP_PORT`. Stop it or choose another port, and change the host nginx
`proxy_pass` to match.

### `Worker failed to boot` with an `IndexError` from pathlib

Fixed. `src/core/config.py` found the repository root by counting directories
up from its own file, which is right in a checkout and wrong in the image,
where `src/` sits directly at `/app`. It raised during import, before any
logger existed. It could not appear in development because hybrid mode runs
from the checkout — the deployed image was the first thing ever to import that
module from an installed layout. `tests/unit/test_config_roots.py` now tests
both layouts as data.

### Frontend container restarting: `open() "/run/nginx.pid" failed (13: Permission denied)`

Fixed. The image ran `chown -R nginx:nginx /var/run`, which does nothing
useful: `/var/run` is a symlink to `/run`, and `chown -R` does not traverse
symlinks, so it retargeted the link and left `/run` owned by root. nginx logged
its version and worker method, then died on the last step before serving. The
Dockerfile now creates the pid file and gives nginx that one file.

### `container atcraft-main-celery-beat-1 is unhealthy`

Fixed. Beat inherited the api image's `HEALTHCHECK` — an HTTP probe of
`/health/live` — and serves no HTTP, so it was permanently unhealthy: `up
--wait` never returned and every monitor showed a broken container that was
working perfectly. It now checks the freshness of its own schedule file, which
is the one honest signal that beat has stopped scheduling while its process is
still alive.

### 502 from the host nginx

The deployment is down or unhealthy:

```bash
./scripts/deploy.sh main health
curl -s http://127.0.0.1:8081/health/ready
sudo tail -50 /var/log/nginx/atcraft-main.error.log
```

### Rate limiting behaves as though every visitor is one client

Check that the deployment nginx still has its `set_real_ip_from` block. The
application uses `X-Real-IP` as the rate-limit identity and as the audit login
IP. Behind the host nginx, without the realip module, `$remote_addr` is the
Docker bridge gateway — identical for every visitor — so per-IP rate limiting
collapses into one shared bucket and every audit row records the same useless
address.

### `pg_isready` fails but the container is running

Almost always `POSTGRES_PASSWORD` changed in the env file after the cluster was
initialised. The password lives in the volume. Either restore the old value, or
`ALTER USER` inside the running cluster:

```bash
./scripts/deploy.sh main psql -c "ALTER USER atcraft WITH PASSWORD '<new>';"
```

### Disk full

```bash
docker system df
docker image prune -f                 # dangling images only
du -sh backups/*
```

Never `docker system prune --volumes`. It deletes named volumes not attached to
a running container — which includes a stopped deployment's entire database.

---

## Handover checklist

Complete the ordered commissioning checklist in
[docs/deployment.md](docs/deployment.md#production-commissioning-sequence)
first. This list records final handover evidence and does not replace that
sequence.

Before the client takes over:

- [ ] `./scripts/deploy.sh main doctor` — clean
- [ ] `./scripts/deploy.sh main health` — all containers healthy, readiness 200
- [ ] `deploy/main.env` is mode 600, not in git, and a copy is stored off the
      box
- [ ] `CORS_ORIGINS`, `ALLOWED_HOSTS`, `PUBLIC_WEB_URL`, `EMAIL_FROM_ADDRESS`
      and `SMTP_HOST` hold the real values in the env file — `doctor` names
      any key that still says `example.com`
- [ ] Host nginx: `./scripts/check-host-nginx.sh --installed` passes, which
      also proves the installed config contains no `example.com`
- [ ] `JWT_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`, and `POSTGRES_PASSWORD` were
      freshly generated for this deployment; the credential key also differs
      from the JWT key
- [ ] The deployment contains **no demo data** — `make db-handover` on the source
      database, or verify: `./scripts/deploy.sh main psql -tAc "select count(*) from customers where code like 'DM-%'"` returns 0
- [ ] The seeded administrator's password has been changed
- [ ] `curl -sI "https://$ERP_HOST"` returns 200 with
      `Strict-Transport-Security`
- [ ] `curl -s "https://$ERP_HOST/metrics"` returns 404
- [ ] `curl -s "https://$ERP_HOST/health/ready"` returns 404
- [ ] an unknown Host/SNI is rejected by `00-default.conf`, not routed to main
- [ ] `sudo certbot renew --dry-run` succeeds, `certbot.timer` enabled
- [ ] A nightly backup cron exists, copies off the box, and has a retention rule
- [ ] `./scripts/deploy.sh main backup-verify` succeeds
- [ ] `sudo ufw status` shows only 22, 80, 443
- [ ] `docker ps --format '{{.Ports}}'` shows no `0.0.0.0` publication
