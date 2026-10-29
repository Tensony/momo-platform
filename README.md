# Mobile Money Platform

A Python/Flask mobile-money API backed by PostgreSQL/Aurora, with transaction processing, a double-entry ledger, Cognito authentication, and asynchronous fraud review on AWS.

## Architecture at a glance

The independently deployable units are the ECS Fargate `wallet-api` container and the `saga-steps`, `fraud-engine`, `reconciliation`, and `db-init` Lambda functions. Step Functions coordinates transfer steps. A transactional outbox publishes completed transactions through SNS to an SQS fraud-review queue; failed messages are retried and eventually sent to a monitored dead-letter queue. ECS runs the stateless API in private subnets across two Availability Zones, behind an Application Load Balancer. Aurora stores application state.

See [docs/DESIGN.md](docs/DESIGN.md) for the architecture diagram, service responsibilities, security model, and operational design. The assignment evidence map is in [docs/ASSIGNMENT-TRACEABILITY.md](docs/ASSIGNMENT-TRACEABILITY.md); operational playbooks are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Operations console

The browser UI is a responsive, static HTML operations console at `/demo` (also served at `/`).
For a local static preview, start the existing server with `.\run_demo_ui.bat` and open
`http://127.0.0.1:5050`; this preview server does not proxy API traffic. In the console choose
**Connect**, enter the API base URL and a Cognito access token, and use only wallets permitted to
that identity. Tokens stay in page memory and are cleared on reload or sign-out. For local
cross-origin use, configure the API process with
`CORS_ALLOWED_ORIGINS=http://127.0.0.1:5050`; origins must be explicitly listed, comma-separated.
CORS is disabled when the setting is empty and never reflects unconfigured origins.

The console reads balances, transactions, ledger entries, fraud alerts, disputes, and reports from
the API; it does not seed demonstration data. Payment requests require confirmation and use a
unique idempotency key. If the network outcome is uncertain, the UI blocks another payment and
provides a status lookup for the original key. Unsupported functions (including account profiles,
fraud review actions, and settlement payout lifecycle) are called out in the UI rather than
simulated. The requirement/API mapping and demonstration steps are in
[docs/FRONTEND-TRACEABILITY.md](docs/FRONTEND-TRACEABILITY.md).

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

For Linux/macOS, install dependencies with `python3.12 -m pip install -r requirements-dev.txt`; use `make test`, `make lint`, and `make proofs`.

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

GitHub Actions runs checks for pushes and pull requests. AWS deployment is opt-in: configure the `aws-deploy` environment with required reviewers, set repository variables `AWS_DEPLOY_ENABLED=true`, `AWS_REGION`, `AWS_PROJECT`, `AWS_ENVIRONMENT`, and `ALERT_EMAIL`, and add the `AWS_DEPLOY_ROLE_ARN` secret for an OIDC-trusted role. The deploy job runs only for pushes to `main` after all checks pass. It updates an already-provisioned ECS service with the pushed image digest (using ECS rolling deployment or CodeDeploy when configured); initial infrastructure provisioning remains a separate operation. TLS certificate validation remains enabled for smoke tests unless `ALLOW_SELF_SIGNED_SMOKE_TEST=true` is explicitly set for a demo stack using Terraform's self-signed certificate.

For live proofs, set `AWS_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, and `API_BASE_URL`, configure AWS CLI credentials, then run `python scripts/run_live_proofs.py`. This script temporarily changes fraud and fault-injection configuration and restores the original values afterward; run it only against a deployment you control. TLS certificate verification is enabled by default. `PROOF_INSECURE_TLS=true` opts into disabling verification for a trusted demo endpoint with a self-signed certificate only.

## Repository map

- `src/momo/` — API, ledger, saga, fraud, outbox, and reconciliation services
- `infra/terraform/` — AWS network, compute, database, messaging, identity, and monitoring
- `tests/` — unit and PostgreSQL integration tests
- `scripts/proofs/` — reproducible correctness proofs
- `statemachine/` — Step Functions saga definition
- `docs/` — design, assignment traceability, runbook, and evidence
