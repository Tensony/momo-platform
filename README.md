# Mobile Money Platform

A Python/Flask mobile-money API backed by PostgreSQL/Aurora, with transaction processing, a double-entry ledger, Cognito authentication, and asynchronous fraud review on AWS.

## Architecture at a glance

The independently deployable units are the ECS Fargate `wallet-api` container and the `saga-steps`, `fraud-engine`, `reconciliation`, and `db-init` Lambda functions. Step Functions coordinates transfer steps. A transactional outbox publishes completed transactions through SNS to an SQS fraud-review queue; failed messages are retried and eventually sent to a monitored dead-letter queue. ECS runs the stateless API in private subnets across two Availability Zones, behind an Application Load Balancer. Aurora stores application state.

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture diagram, service responsibilities, security model, and operational design. The assignment evidence map is in [docs/ASSIGNMENT-TRACEABILITY.md](docs/ASSIGNMENT-TRACEABILITY.md); operational playbooks are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Web interface and operations console

The public landing page is served at `/`, with separate sign-in (`/login`), customer registration
(`/register`), and role-specific customer, agent, and administrator workspaces. Use the API-hosted
pages so authentication and service requests go to the same origin. The previous integrated
operations console remains available at `/demo` (also `/console`) for its additional wallet,
ledger, dispute, fraud-alert, and reconciliation views.

For local development, start the API with the database setup above in one terminal. In another,
start the frontend proxy:

```powershell
$env:MOMO_API_BASE_URL = "http://127.0.0.1:8080"
.\run_demo_ui.bat
```

Open the URL printed by the server (normally `http://127.0.0.1:5050`). The frontend server proxies
the role login, session, wallet, transaction, and administrative API routes to the Flask API, so
browser requests remain same-origin and do not require CORS configuration. Set
`MOMO_API_BASE_URL` to another HTTP(S) API origin, such as `http://127.0.0.1:5080`, when needed.
The backend must be running; an unavailable backend returns an explicit `502` instead of demo data.
The separate in-memory simulation routes under `/api/*` are not the authenticated transaction API.

The role-specific workspaces and console read balances and transaction results from the API; they
do not seed demonstration data or calculate authoritative balances. Payment requests require
confirmation and use idempotency keys. If a standalone workspace receives an uncertain outcome, it
blocks repeat submission and offers a status lookup for the original key. Unsupported functions
(including profile editing, administrator user management, and agent-initiated cash-out) are
identified rather than simulated. The requirement/API mapping and demonstration steps are in
[docs/FRONTEND-TRACEABILITY.md](docs/FRONTEND-TRACEABILITY.md); the dated browser and role-check
results are in [docs/UI_AUDIT_AND_COMPLIANCE_REPORT.md](docs/UI_AUDIT_AND_COMPLIANCE_REPORT.md).

The optional in-memory interactive demo API (`/api/state`, `/api/reset`, and related simulation
routes) is disabled by default in the Flask API. Enable it only for a trusted local demo by setting
`MOMO_DEMO_API_ENABLED=true`; do not enable it on a publicly reachable deployment. It uses seeded,
in-memory demo wallets and is not the production transaction API.

## Local development and verification (Windows)

Requirements: Python 3.12 and Docker Desktop. Start a throwaway local PostgreSQL database:

```powershell
docker run --name momo-postgres -d -p 127.0.0.1:5432:5432 `
  -e POSTGRES_HOST_AUTH_METHOD=trust `
  -e POSTGRES_DB=momo_test postgres:16
```

Create a virtual environment and install development dependencies:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Run the unit and PostgreSQL integration tests, lint, and correctness proofs:

```powershell
.\make.bat test
.\make.bat lint
.\make.bat proofs
```

The test runner uses `TEST_DATABASE_URL` if set; otherwise, it connects to the local test database. To override it in PowerShell:

```powershell
$env:TEST_DATABASE_URL = "postgresql://postgres@localhost:5432/momo_test"
```

Stop the local database when finished:

```powershell
docker stop momo-postgres
```

For Linux/macOS, install dependencies with `python3.12 -m pip install -r requirements-dev.txt`; use `make test`, `make lint`, and `make proofs`. Start the API and frontend proxy in separate terminals, setting `MOMO_API_BASE_URL` before starting `scripts/demo_server.py` if the API is not at `http://127.0.0.1:8080`.

### Run the API and role-based login locally (Windows)

With the `momo-postgres` container from the setup above running, apply the schema and start the API:

```powershell
docker cp db\schema.sql momo-postgres:/tmp/momo-schema.sql
docker exec momo-postgres psql -U postgres -d momo_test -f /tmp/momo-schema.sql

$env:DATABASE_URL = "postgresql://postgres@localhost:5432/momo_test"
$env:JWT_SECRET = (.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))")
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -c "from momo.api.app import create_app; create_app().run(host='127.0.0.1', port=8080)"
```

Open `http://127.0.0.1:8080/` for the landing page, then use **Create account** or **Sign in**.
Agent and administrator accounts are intentionally provisioned only by an operator; from a trusted
environment connected to the database, run
`.\.venv\Scripts\python.exe scripts\provision_user.py agent --identifier AGENT-007 --full-name "Local Agent"`
or use `admin` as the role. The script prompts for the initial password and never accepts it as a
command-line argument. Keep the same `JWT_SECRET` for the lifetime of the local database so existing
sessions remain verifiable.

The local role access tokens expire after one hour. Role workspaces verify identity with
`/auth/me`, while the API continues to enforce endpoint authorization. The standalone static
preview (`.\run_demo_ui.bat`) remains UI-only and cannot perform API authentication.

## Terraform and AWS deployment

Terraform configuration is in [`infra/terraform/`](infra/terraform/). To validate configuration without creating AWS resources:

```powershell
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform init -backend=false -input=false
terraform -chdir=infra/terraform validate
```

For a real deployment, install AWS CLI and Docker, configure AWS credentials (set `AWS_PROFILE` if using a named local profile), and use Bash (WSL or Git Bash) because `scripts/deploy.sh` is a Bash script:

```bash
scripts/deploy.sh you@example.com <selected-region>
```

This provisions billable AWS resources. Confirm the project's Region in **AWS Settings → View all projects → Overview → Additional Info → Region**, and review your plan and billing limits before deploying. The Terraform default is `af-south-1`; pass your selected Region explicitly when it differs. A deployment has **not** been verified merely by running Terraform validation; see the traceability document for the evidence boundary.

Terraform provisions the platform JWT signing key in AWS Secrets Manager and injects it into the API task; protect Terraform state because it contains sensitive generated infrastructure values.

GitHub Actions runs on pull requests targeting `main` and pushes to `main`. It runs the secret scan, Ruff, PostgreSQL tests with coverage output, local correctness proofs, Terraform format/validation, and `tfsec --soft-fail`. After successful checks on a push to `main`, the deploy job builds the image and uses the `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN` repository secrets when they are active to push to ECR and update the pre-provisioned ECS service. If those credentials are unavailable, it skips the live push. This workflow does not use OIDC or an opt-in deployment environment; initial infrastructure provisioning remains separate. A successful local validation is not evidence of a live deployment. Avoid pushing to `main` solely to rerun CI unless deployment is intended.

For live proofs, set `AWS_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, and `API_BASE_URL`, configure AWS CLI credentials, then run `python scripts/run_live_proofs.py`. This script temporarily changes fraud and fault-injection configuration and restores the original values afterward; run it only against a deployment you control. TLS certificate verification is enabled by default. `PROOF_INSECURE_TLS=true` opts into disabling verification for a trusted demo endpoint with a self-signed certificate only.

## Repository map

- `src/momo/` — API, ledger, saga, fraud, outbox, and reconciliation services
- `infra/terraform/` — AWS network, compute, database, messaging, identity, and monitoring
- `tests/` — unit and PostgreSQL integration tests
- `scripts/proofs/` — reproducible correctness proofs
- `statemachine/` — Step Functions saga definition
- `docs/` — design, assignment traceability, runbook, and evidence
