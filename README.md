# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included
worker deterministically returns `input.upper()`.

## Run it

```bash
docker compose up --build
```

This starts PostgreSQL and the API together (service names `postgres` and
`agent-relay`); the API waits for the database's healthcheck before starting.
Open <http://127.0.0.1:8000/> for the token-based local dashboard. `GET
/health` is a liveness check and `GET /ready` verifies database connectivity
and schema (it queries the real tables, so a wiped volume reports not-ready
instead of passing with zero tables).

If port `5432` or `8000` is already taken on your machine, remap them without
editing the file:

```bash
POSTGRES_PORT=5433 API_PORT=8001 docker compose up --build
```

To run the API on the host instead of in a container (useful for `--reload`),
start only PostgreSQL and point the app at its published port:

```bash
docker compose up -d postgres
uv sync
uv run uvicorn main:app --reload
```

The default `RELAY_DATABASE_URL`
(`postgresql+psycopg://postgres:postgres@localhost:5432/agent_relay`) matches
that published port. Set `RELAY_DATABASE_URL` to point at a different
PostgreSQL instance instead.

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains SQLAlchemy models and engine setup. `storage.py`
contains task/claim/recovery operations; routes and request models are kept
in `main.py` and `schemas.py`. Claim, heartbeat, terminal submission, and
recovery each take row-level locks on exactly the rows they touch
(`SELECT ... FOR UPDATE` / `FOR UPDATE SKIP LOCKED`), so concurrent claims
across API processes are coordinated by PostgreSQL rather than a
process-local lock.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
docker compose up -d postgres
uv run pytest -q
```

Tests default to a separate `agent_relay_test` database on the same
PostgreSQL instance, created by `initdb/001-create-test-db.sql` the first
time the `postgres` service's data volume is initialized (delete the
`postgres-data` volume with `docker compose down -v` to force it to run
again). The fixture drops and recreates all tables on whatever
`RELAY_DATABASE_URL` points at, so don't run tests against a database with
data you need; set `RELAY_DATABASE_URL` explicitly to override the default.

`test_integration_live.py` is a separate, black-box check that speaks real
HTTP to whatever server is already running at `RELAY_BASE_URL` (default
`http://127.0.0.1:8000`); it skips itself if none is reachable.

This starter intentionally does not include CI, external brokers, or an LLM.
Those are deployment concerns rather than part of the local relay protocol.

## Run it on Kubernetes (kind)

```bash
brew install kind          # kubectl is assumed to already be installed
kind create cluster --name agent-relay --config kind-config.yaml
docker build -t agent-relay:local .
kind load docker-image agent-relay:local --name agent-relay
kubectl apply -f k8s/
kubectl -n agent-relay rollout status deployment/postgres
kubectl -n agent-relay rollout status deployment/agent-relay
```

Open <http://localhost:8080/> — `kind-config.yaml` maps the cluster node's
NodePort 30080 (set on the `agent-relay` Service) to that host port. `k8s/`
contains a `postgres` Deployment backed by a `PersistentVolumeClaim` (data
survives pod restarts/rescheduling) and a two-replica `agent-relay`
Deployment, both with `livenessProbe`/`readinessProbe`s wired to `/health`
and `/ready`. The Postgres Service is named `postgres`, so
`RELAY_DATABASE_URL` (in `k8s/postgres-secret.yaml`) resolves it the same way
`compose.yaml` does.

`kind load docker-image` copies the image straight into the cluster's own
container runtime — there is no registry, so the Deployments set
`imagePullPolicy: Never`. Rebuild and reload after code changes, then roll
the deployment:

```bash
docker build -t agent-relay:local .
kind load docker-image agent-relay:local --name agent-relay
kubectl -n agent-relay rollout restart deployment/agent-relay
```

Tear down with `kind delete cluster --name agent-relay`.
