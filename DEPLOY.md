# Deploy

## Local development

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env       # fill in DATABASE_URL + JWT_SECRET
uvicorn app.main:app --reload --port 8000
```

OpenAPI explorer: <http://localhost:8000/docs>

## Seeding

The Node side already populates the master data via `npm run db:reset` in `safeops_360`. Run the Python seeders **only if you want them to own the data going forward**:

```bash
python -m app.seed.seed_workflows   # idempotent — workflow definitions
python -m app.seed.seed_rbac        # idempotent — roles + permissions + grants
```

Once the cutover is complete you can stop running the Node seeders.

## Docker (single container)

```bash
docker build -t safeops360-backend .
docker run --rm -p 8000:8000 --env-file .env safeops360-backend
```

## Render

`render.yaml` skeleton:

```yaml
services:
  - type: web
    name: safeops360-backend
    runtime: docker
    plan: starter
    region: singapore  # match Supabase ap-southeast-1 latency
    healthCheckPath: /health
    envVars:
      - key: DATABASE_URL
        sync: false  # set in dashboard from Supabase pooler
      - key: JWT_SECRET
        generateValue: true
      - key: SUPABASE_URL
        sync: false
      - key: SUPABASE_SERVICE_ROLE_KEY
        sync: false
      - key: CORS_ORIGINS
        value: https://your-frontend.vercel.app
      - key: APP_ENV
        value: production
```

## Fly.io

`fly.toml` skeleton:

```toml
app = "safeops360-backend"
primary_region = "sin"

[build]
  dockerfile = "Dockerfile"

[env]
  APP_ENV = "production"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = false
  min_machines_running = 1

[[http_service.checks]]
  interval = "30s"
  timeout = "5s"
  grace_period = "10s"
  method = "GET"
  path = "/health"
```

Set secrets:
```bash
fly secrets set DATABASE_URL=... JWT_SECRET=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... CORS_ORIGINS=https://your-frontend.vercel.app
fly deploy
```

## Cutover plan

1. **Stage 1 — coexist (current)**: Both Node and Python services run. Frontend has `BACKEND_URL` unset; everything stays on Node + Prisma.
2. **Stage 2 — opt-in by module**: Set `BACKEND_URL` in the Next.js env. Auth flows through Python automatically. Migrate one server-component page at a time to call `backendFetch()`. Rollback per-page is just removing `backendFetch()` and calling Prisma again.
3. **Stage 3 — flip**: Once every server component uses `backendFetch()`, the Node API routes under `/api/*` are dead code. Delete them.
4. **Stage 4 — Alembic owns the schema**: Stop running `prisma db push`; future schema changes are Alembic revisions. The Prisma schema can stay as documentation or be removed.

Don't skip stage 2 — going straight from Node+Prisma everywhere to Python everywhere in production is the riskiest path.

## Migrations

Initial autogenerate (run once after first deploy with `BACKEND_URL` set):

```bash
alembic revision --autogenerate -m "initial — sync to existing schema"
```

Inspect the diff carefully. If Prisma already created the schema correctly, the diff should be near-empty (some Postgres enum vs SQLAlchemy enum naming nits may show up — review and adjust manually). Then:

```bash
alembic upgrade head
```

After this point, schema changes are: edit `app/models/*.py` → `alembic revision --autogenerate -m "..."` → review → `alembic upgrade head`.

## Operations

- **Logs**: stdout/stderr — `docker logs` / Render Logs / `fly logs` all work.
- **Healthcheck**: `GET /health` returns `{"status":"ok","env":"production"}`.
- **Shell**: `fly ssh console` or `docker exec` — useful for `python -m app.seed.seed_rbac` after a permission matrix change.

## Required environment variables

| Name | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | `postgresql+asyncpg://...` (Supabase pooler) |
| `DATABASE_URL_SYNC` | for Alembic | `postgresql+psycopg2://...` (same db) |
| `JWT_SECRET` | yes | 32+ byte random string |
| `JWT_ALGORITHM` | no | default HS256 |
| `ACCESS_TOKEN_TTL_MINUTES` | no | default 720 (12h) |
| `SUPABASE_URL` | for incident attachments | from Supabase project settings |
| `SUPABASE_SERVICE_ROLE_KEY` | for incident attachments | service-role JWT — never expose |
| `SUPABASE_INCIDENT_BUCKET` | no | default `incident-attachments` |
| `CORS_ORIGINS` | yes | comma-separated; must include the deployed frontend URL |
| `APP_ENV` | no | `production` flips off debug mode |

## Frontend env

In `safeops_360/.env.local` (or `.env.production` for Vercel):

```
BACKEND_URL=https://safeops360-backend.example.com
NEXTAUTH_URL=https://your-frontend.example.com
NEXTAUTH_SECRET=<unchanged>
```

That's it. Once `BACKEND_URL` is set, every login round-trips through Python and the frontend has the bearer token ready for `backendFetch()`.
