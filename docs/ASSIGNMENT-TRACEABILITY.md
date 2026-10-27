# Assignment requirements traceability

This matrix maps each assessment requirement to its implementation and repeatable repository checks. It distinguishes configuration and automated-test evidence from evidence that requires resources to be deployed in AWS.

| Requirement | Implementation | Reproducible verification | Evidence boundary |
|---|---|---|---|
| At least three independently deployable services with clear responsibilities | `wallet-api` (ECS), `saga-steps`, `fraud-engine`, `reconciliation`, and `db-init` (Lambda); separate images/artifacts, handlers, and IAM roles are defined in `infra/terraform/ecs.tf` and `infra/terraform/lambdas.tf`. Responsibilities and triggers are described in [DESIGN.md](DESIGN.md#21-independently-deployable-services-requirement--3). | `terraform -chdir=infra/terraform validate`; `python scripts/build_lambdas.py`; inspect service handler tests in `tests/integration/`. | Configuration verifies distinct deploy units; independently deployed AWS services require a successful deployment. |
| Asynchronous processing with reliable delivery, retries, and recovery | `finalize` commits a `TransactionCompleted` outbox row with the transaction; `saga-steps` attempts immediate publication, and the scheduled reconciliation sweep retries unpublished rows. SNS fans out to SQS; partial batch failures are returned for retry, then moved to the DLQ. Fraud review is idempotent on `(txn_id, decision)`. | `pytest tests/integration/test_reconciliation_outbox.py`; `python scripts/proofs/run_proofs.py --local`. | Tests exercise database and handler behavior; AWS delivery, EventBridge scheduling, and DLQ alarms require live AWS evidence. |
| Stateless, horizontally scalable application tier | API correctness state is in Aurora; ECS task definitions use a read-only root filesystem, at least two tasks, AZ rebalancing, and CPU/request-count autoscaling. | `terraform -chdir=infra/terraform validate`; inspect `infra/terraform/ecs.tf` and `Dockerfile`; run the API and idempotency integration tests. | Horizontal scaling behavior must be demonstrated against deployed ECS tasks to claim runtime evidence. |
| Deployment across at least two Availability Zones | Terraform creates two public, app, and data subnets using two available AZs; the ALB uses both public subnets and ECS/Lambda use both app subnets. The API minimum task count is validated as at least two. | `terraform -chdir=infra/terraform validate`; inspect `infra/terraform/network.tf`, `infra/terraform/ecs.tf`, and `infra/terraform/variables.tf`. | This verifies intended Terraform configuration only. A deployment plan/apply and AWS resource inventory are required to prove actual AZ placement. |
| Tests for transaction processing, idempotency, authentication, authorization, and failures | Saga compensation, concurrency and ledger invariants, replay semantics, JWT claim/signature rejection, ownership/group authorization, and async retries have unit/integration coverage. | `.\make.bat test` on Windows or `make test` on Linux/macOS with PostgreSQL 16; `.\make.bat proofs` / `make proofs`. | Run results prove behavior in the tested local environment, not AWS service availability. |
| CI/CD validation, infrastructure security, and deployment safeguards | GitHub Actions runs gitleaks, Ruff, PostgreSQL tests/coverage, correctness proofs, Terraform formatting/validation, and a blocking tfsec scan. AWS deployment uses OIDC, is opt-in, is restricted to `main`, and is gated by the `aws-deploy` environment. | Review `.github/workflows/ci-cd.yml`; use a pull request to execute non-deployment checks. | AWS deployment additionally requires repository configuration, an OIDC trust role, protected environment approval, and a pre-provisioned stack. |
| Documentation and repeatable verification | This matrix, [README.md](../README.md), [DESIGN.md](DESIGN.md), and [RUNBOOK.md](RUNBOOK.md) describe requirements, architecture, deployment, tests, and operations. | Follow the local and Terraform validation commands in [README.md](../README.md). | The repository does not contain evidence of a current AWS deployment. Do not mark deployment-dependent criteria as demonstrated without adding dated AWS evidence. |

## Verification commands

From the repository root, with Python 3.12, development dependencies, and a local PostgreSQL 16 test database available:

```powershell
.\make.bat test
.\make.bat lint
.\make.bat proofs
```

Terraform checks do not create resources:

```powershell
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform init -backend=false -input=false
terraform -chdir=infra/terraform validate
```

The CI Terraform security scan is blocking. Apply only after confirming the selected AWS Region, credentials, cost limits, and the Terraform plan; actual AWS state and scaling evidence must be recorded separately.
