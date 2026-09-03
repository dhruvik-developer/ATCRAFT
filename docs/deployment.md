# Deployment

Master rules sections 45–50, 70, 71.

Target: a single dedicated server, roughly 100 GB of storage, running the whole
stack under Docker Compose. That is an appropriate starting point and should
evolve only when measurement says it must.

> **Read `ONE-BOX-RUNBOOK.md` first.** The box now runs **two independent
> deployments** (main and dev), and the runbook is the operational document:
> exact commands, the isolation model, and the verification script.
>
> Superseded here: the single-stack topology below, TLS terminating in the
> compose nginx, and `make prod-up`. TLS now terminates in a **host** nginx that
> routes two domains to two loopback ports, and deployments are driven only by
> `./scripts/deploy.sh <main|dev> <command>`.
>
> Still current, and not repeated in the runbook: the storage sizing analysis,
> what to look for when reviewing a migration before it goes near production,
> and the rollback discussion.

---

## Topology

```
Internet
   │  443
   ▼
 Nginx  ── TLS · rate limit · security headers · compression
   │
   ├── /       → frontend container (static, nginx)
   └── /api    → backend container (gunicorn + uvicorn workers)
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   PostgreSQL      Redis      Celery worker + beat
   (volume)       (volume)          │
        └────────────┬───────────────┘
                     ▼
              storage volume
```

Only Nginx publishes ports. Postgres and Redis are reachable only on the internal
Compose network.

---

## First deployment

### 1. Server preparation

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"          # log out and back in

sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

### 2. Get the code

```bash
sudo mkdir -p /opt/atcraft && sudo chown "$USER" /opt/atcraft
git clone <repository-url> /opt/atcraft && cd /opt/atcraft
git checkout main
```

### 3. Configure

```bash
cp .env.example .env
python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(64))"
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32))"
```

Edit `.env`:

```ini
APP_ENV=production
APP_DEBUG=false
LOG_JSON=true
JWT_SECRET=<generated>
POSTGRES_PASSWORD=<generated>
DATABASE_URL=postgresql+asyncpg://atcraft:<password>@postgres:5432/atcraft_erp
DATABASE_SYNC_URL=postgresql+psycopg://atcraft:<password>@postgres:5432/atcraft_erp
CORS_ORIGINS=https://erp.atcraft.example
STORAGE_PATH=/storage
```

```bash
chmod 600 .env
```

The application refuses to start in production with a weak `JWT_SECRET`, with
`APP_DEBUG=true`, or with a wildcard CORS origin alongside credentials. That is
deliberate — a misconfigured production deployment is an incident.

### 4. TLS

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d erp.atcraft.example
sudo mkdir -p /opt/atcraft/infrastructure/nginx/certs
sudo cp /etc/letsencrypt/live/erp.atcraft.example/fullchain.pem \
        /opt/atcraft/infrastructure/nginx/certs/
sudo cp /etc/letsencrypt/live/erp.atcraft.example/privkey.pem \
        /opt/atcraft/infrastructure/nginx/certs/
```

Add a renewal hook that copies the new certificates and reloads Nginx.

### 5. Start

```bash
cd /opt/atcraft
set -a; source .env; set +a
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

### 6. Schema and baseline data

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec backend alembic upgrade head

docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec backend python -m src.core.seed --company "Atcraft" --admin-email ops@atcraft.example
```

The seed prints a one-time administrator password and sets
`must_change_password`. Change it at first sign-in.

### 7. Verify

```bash
curl -fsS https://erp.atcraft.example/healthz
curl -fsS https://erp.atcraft.example/api/v1/../health/ready   # via the backend
docker compose ... logs --tail=50 backend celery-worker
```

Confirm `/docs` returns 404 in production.

---

## Storage: 100 GB is not 100 GB

Do not assume the application gets all of it.

| Consumer | Reserve |
|---|---|
| OS and packages | ~15 GB |
| Docker images and layers | ~10 GB |
| PostgreSQL data | ~30 GB |
| PostgreSQL WAL | ~5 GB |
| Uploaded files | ~20 GB |
| Logs | ~5 GB |
| Working headroom | ~15 GB |

Monitor all of them. Log rotation is configured in the production compose file
(20 MB × 5 per service); temporary files are purged by a scheduled task. Disk
exhaustion takes PostgreSQL down hard, so alert at 80 %.

**Backups do not live here.** See below.

---

## Backups

```bash
# scripted daily, then copied OFF this machine
docker compose ... exec -T postgres \
  pg_dump -U atcraft -Fc atcraft_erp > /opt/atcraft/backups/atcraft-$(date +%F).dump
```

Requirements, all of them:

- automated and scheduled, not manual;
- a retention policy — daily for 14 days, weekly for 8 weeks, monthly for 12;
- **an external destination.** The master rules are blunt: never keep the only
  backup on the production server;
- the `storage/` volume backed up too — the database references files that must
  still exist;
- **restore tested.** A backup is not a backup until a restore has been performed
  into a scratch environment and the application has come up against it. Do this
  quarterly and record the date.

```bash
# restore drill
docker compose ... exec -T postgres \
  pg_restore -U atcraft -d atcraft_erp_restore_test --clean --if-exists < backup.dump
```

---

## Updates

```bash
cd /opt/atcraft
git pull origin main

docker compose ... exec -T postgres pg_dump -U atcraft -Fc atcraft_erp > backups/pre-deploy.dump

docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend alembic upgrade head
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

curl -fsS https://erp.atcraft.example/healthz
docker compose ... logs --tail=100 backend
```

Migrations run **before** the new application starts, so the running code never
meets a schema it does not expect.

### Reviewing a migration before it goes near production

- Does any statement take an `ACCESS EXCLUSIVE` lock on a large table? Prefer
  `CREATE INDEX CONCURRENTLY` and `ADD COLUMN … NULL` plus a batched backfill.
- Is existing data given correct values?
- Does `downgrade()` actually restore the previous state, or lose data? Say so
  explicitly if it does.
- How long will it run against real row counts?

Never edit a production schema by hand.

### Rollback

```bash
git checkout <previous-tag>
docker compose ... build && docker compose ... up -d
# only if the schema changed and the rollback requires it:
docker compose ... exec backend alembic downgrade -1
```

If a migration was destructive, restore `pre-deploy.dump` instead.

---

## Operations

```bash
# health
docker compose ... ps
curl -fsS localhost/healthz

# logs (structured JSON in production)
docker compose ... logs -f --tail=100 backend
docker compose ... logs -f celery-worker

# queue depth
docker compose ... exec redis redis-cli llen celery
docker compose ... exec celery-worker celery -A src.workers.celery_app:celery_app inspect active

# slow queries
docker compose ... exec postgres psql -U atcraft -d atcraft_erp -c \
 "SELECT calls, round(mean_exec_time::numeric,1) ms, round(total_exec_time::numeric) total, query
    FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;"

# disk
df -h && docker system df
```

Metrics are exposed on the backend's `/metrics` for Prometheus. Grafana and
Prometheus are not part of this stack — point an existing installation at it, or
add them when there is someone to watch the dashboards.

---

## Scaling, in order

Do not skip steps, and do not add infrastructure ahead of evidence.

1. Fix SQL and indexes.
2. Add Redis caching where it is safe.
3. Move heavy work to Celery.
4. Increase server resources.
5. Move PostgreSQL to its own host.
6. Add read replicas for reporting.
7. Separate a genuinely hot component.
8. Consider microservices — only when real scale or team structure demands it.

---

## Go-live checklist

**Security**
- [ ] `JWT_SECRET` generated, unique, 64 bytes
- [ ] `APP_DEBUG=false`, `APP_ENV=production`, `LOG_JSON=true`
- [ ] database password rotated off the development default
- [ ] `CORS_ORIGINS` set to the real origin, no wildcard
- [ ] TLS working; HTTP redirects; HSTS present
- [ ] `.env` is `chmod 600` and not in git
- [ ] Postgres and Redis not published to the host
- [ ] `/docs`, `/redoc`, `/openapi.json` return 404
- [ ] seeded admin password changed
- [ ] firewall allows only 22, 80, 443

**Data**
- [ ] migrations applied; `alembic current` matches head
- [ ] baseline data seeded
- [ ] audit-log immutability trigger present in the deployed schema
- [ ] automated backups running to **external** storage
- [ ] a restore has actually been tested

**Operations**
- [ ] all containers healthy; restart policies in place
- [ ] log rotation configured
- [ ] disk usage alerting at 80 %
- [ ] `/metrics` scraped
- [ ] Sentry DSN set, or the decision not to use it recorded
- [ ] someone knows how to read the logs at 2 a.m.

**Verification**
- [ ] end-to-end journey passes against production: log in → create item → create
      BOM → sales order → run MRP → purchase order → GRN → production → QC →
      dispatch
- [ ] a load test has been run at expected concurrency
- [ ] permissions verified for each real role, not just the admin
