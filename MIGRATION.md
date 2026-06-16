# SafeOps360 — Node.js → Python migration playbook

This is the strangler migration. Both backends run side-by-side against the same Supabase Postgres. The Next.js side stays mounted as a frontend BFF; one module at a time gets cut over to the Python service behind a feature switch.

## How the cutover works

1. Set `BACKEND_URL=http://localhost:8000` in the Next.js `.env`.
2. With that variable set:
   - NextAuth's credentials provider POSTs to **Python** `/api/auth/login` instead of querying Prisma. The Python access token is stored on `session.user.backendAccessToken`.
   - Any server component that uses `backendFetch()` gets data from Python.
3. Without `BACKEND_URL` set: the Next.js side runs entirely against Prisma as before. This means you can leave production on Node while migrating module by module locally.

`backendFetch()` is in [`src/lib/backend.ts`](../safeops_360/src/lib/backend.ts) and forwards the bearer token automatically.

## Module migration recipe

For each module, do these in order:

1. **Build the Python router** following the [Observations template](app/routers/observations.py).
2. **Mount it** on `app.main:app`.
3. **Verify with curl** that the endpoints enforce auth + RBAC.
4. **Swap the Next.js side**: replace the direct `prisma.X` call in the server component with `await backendFetch('/api/X')`. Keep the Prisma fallback in a comment so a `git diff` makes the cutover obvious.
5. **Smoke-test** with all roles you care about (Worker, HSE Manager, Plant Head, Corporate HSE).

## Running both services in dev

```bash
# Terminal 1 — Python backend
cd safeops_360_bakend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000

# Terminal 2 — Next.js frontend
cd safeops_360
# add BACKEND_URL=http://localhost:8000 to .env.local
npm run dev
```

Visit <http://localhost:3000>; login flows through to the Python backend. Visit <http://localhost:8000/docs> for the OpenAPI explorer.

## What's done in session 1

| Layer | Status |
|---|---|
| Project scaffold | ✅ |
| Config + async DB engine + JWT + bcrypt + FastAPI deps | ✅ |
| Full SQLAlchemy schema (all 30+ tables matching Prisma) | ✅ |
| Permission service (port of `permissions.ts`) | ✅ |
| FLRA gate (port of `flra-gate.ts`) | ✅ |
| Workflow engine (port of `engine.ts`) | ✅ |
| Auth router (`/api/auth/login`, `/api/auth/permissions`, `/api/auth/me`) | ✅ |
| Observations router (CRUD + workflow init + plant scoping) — **the template** | ✅ |
| Alembic config | ✅ |
| Next.js `auth.ts` wired to Python via `BACKEND_URL` | ✅ |
| Next.js `backendFetch()` helper for server components | ✅ |

## What's queued for sessions 2–4

- **Session 2**: Near-Miss + PTW + FLRA routers (incl. `/sign`, `/redo`, `/suspend`, `/resume`, `/eligible-for-flra`); Supabase Storage helper for incident attachments.
- **Session 3**: Incidents (incl. RCA) + Training + Inspections + Manhours routers; Workflow API (`/approve`, `/reject`, `/submit-execution`, `/verify`, `/resubmit`, `/reassign`, `/my-count`, `/definitions/*`); seeders ported (Python equivalents of the three TS seeders).
- **Session 4**: Users router; deploy config (Dockerfile, env mapping, Render/Fly compose); cutover plan; remove Node API routes; remove Prisma from runtime path (kept only for `prisma db push` until Alembic owns the schema).

## When Alembic should take over

Right now the Node side still runs `prisma db push`. The Python side has Alembic configured but no initial migration committed — that's deliberate, because the schema is already correct in the DB. When you're ready:

```bash
# from safeops_360_bakend/
alembic revision --autogenerate -m "initial — match Prisma schema"
# review the diff carefully — should be near-empty if Prisma + SQLAlchemy agree
alembic upgrade head
```

After that point: stop running `prisma db push`; schema changes go through Alembic.

## Auth flow under `BACKEND_URL` mode

```
Browser  ───POST /api/auth/[…nextauth]───▶  Next.js
                                              │
                                              │ POST /api/auth/login
                                              ▼
                                            Python (FastAPI)
                                              │
                                              │ verify(email, bcrypt(password))
                                              ▼
                                            Postgres
                                              │
                                              ▼ access_token (JWT, 12h)
                                            Python
                                              │
                                              ▼
                                            Next.js JWT callback
                                              │ stash backendAccessToken
                                              ▼
                                            Browser cookie
```

Subsequent server-component requests:

```
ServerComponent  ─── backendFetch('/api/X') ───▶ Python
                       Authorization: Bearer …
```

## Notes on the schema port

- `User.role` is kept as a denormalised string column for back-compat; the source of truth for permissions is `UserRole → Role → RolePermission`.
- All cuid IDs on existing rows are preserved. New rows minted by Python use uuid4 hex via `gen_id()` in [`app/models/_base.py`](app/models/_base.py).
- Postgres-native enum types (`PermitStatus`, `IncidentType`, etc.) are reused. SQLAlchemy `Enum` declares the same names so Alembic doesn't try to recreate them.
