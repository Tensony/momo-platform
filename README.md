# Mobile Money Transaction Platform (AWS)

Cloud-native wallet / transfer / agent / merchant platform with an immutable ledger, saga-based
distributed transactions, idempotency and fraud rules. Built for the Cloud Application Development mini project.

**Start with `docs/DESIGN.md`** (architecture, security, SLOs, cost, requirement traceability),
then `docs/TEAM_PLAN.md` (what to do before Thursday) and `docs/RUNBOOK.md` (operations).

## What has and hasn't been verified

| Claim | Status |
|---|---|
| 113 unit + integration tests pass, **92 % coverage** (gate 60 %), against a real PostgreSQL 16 | ✅ run |
| Proof 1 — 100 replays of one request ⇒ exactly **one** debit | ✅ run (local: in-process API + PostgreSQL) |
| Proof 2 — crash injected at 5 points of the saga ⇒ **no money created/destroyed** | ✅ run (local) |
| Proof 3 — 50 concurrent transfers, funds for 33 ⇒ **33 succeed, 17 rejected, never overdrawn** | ✅ run (local) |
| Hash-chain tamper detection; append-only ledger triggers; deadlock-free opposite transfers | ✅ tested |
| Secret scan | ✅ `gitleaks` over the tracked files, default rules, no allowlist: **no leaks found** |
| Lambda artifacts build for Linux (`scripts/build_lambdas.sh`) | ✅ built; psycopg binary is manylinux x86-64 / cp312 |
| Terraform (127 resources) | ⚠️ HCL syntax, formatting and every cross-reference checked offline. **Not applied and not schema-validated against the AWS provider** (the provider registry was unreachable). Run `terraform validate` first and expect to fix small version issues. |
| Proofs against **AWS**, autoscaling demo, rollback demo, CloudTrail, cost actuals | ⏳ need your AWS account — commands below; `docs/TEAM_PLAN.md` has the schedule |
| Cost figures | ⚠️ **estimates** from `scripts/cost_model.py`; replace with Cost Explorer actuals (DESIGN §10) |

> **QLDB is discontinued by AWS** (support ended 2025), so the immutable ledger is PostgreSQL: append-only triggers, revoked UPDATE/DELETE, and a per-wallet SHA-256 hash chain. Justified in DESIGN §3.

## Run locally (no AWS needed)

```bash
docker run -d --name pg -p 5432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=momo_test postgres:16
make install
make cover        # all tests + coverage gate
make proofs       # the three correctness proofs -> evidence-out/proof-results.md
pytest -k tampering   # edit a ledger row as a superuser: the verifier detects it
python scripts/cost_model.py
```
`TEST_DATABASE_URL` defaults to `postgresql://postgres@localhost/momo_test`.

## Deploy to a clean AWS account

Prereqs: Terraform ≥ 1.6, Docker, AWS CLI with credentials for a **sandbox** account, Python 3.12.

```bash
scripts/deploy.sh you@example.com            # build → ECR → push image → terraform apply (~25 min, Aurora is slow)
# confirm the SNS subscription e-mail AWS sends you (otherwise alarms can't reach you)
eval "$(scripts/get_tokens.sh)"              # creates demo users, exports USER_TOKEN / AGENT_TOKEN / ADMIN_TOKEN
API=$(terraform -chdir=infra/terraform output -raw api_url)     # self-signed cert: tools need -k
curl -k $API/health
```

### Produce the evidence the brief asks for

```bash
# 1-3. correctness proofs against AWS (needs fault injection ON and the velocity rule relaxed)
terraform -chdir=infra/terraform apply -var alert_email=you@example.com -var enable_fault_injection=true
aws ssm put-parameter --name /momo/fraud/config --type String --overwrite --value '{"velocity_max_txns":100000}'
python scripts/proofs/run_proofs.py --base-url $API --admin-token $ADMIN_TOKEN --agent-token $AGENT_TOKEN --user-token $USER_TOKEN
# ...then turn fault injection back OFF and restore the fraud config:
terraform -chdir=infra/terraform apply -var alert_email=you@example.com
aws ssm put-parameter --name /momo/fraud/config --type String --overwrite --value '{}'

# 4. automated rollback (deploys a deliberately unhealthy release; CodeDeploy reverts it)
scripts/rollback_demo.sh

# 5. auto-scaling (create two wallets and fund the payer first - see the k6 file header)
k6 run -e BASE_URL=$API -e TOKEN=$USER_TOKEN -e FROM=<wallet> -e TO=<wallet> load/k6-autoscale.js
scripts/autoscale_evidence.sh
```
Then screenshot the CloudWatch dashboard, alarms and an X-Ray trace, and put everything under `docs/evidence/`.

## CI/CD

`.github/workflows/ci-cd.yml`: gitleaks (full history) → lint → tests + coverage gate → correctness proofs → `terraform validate` + tfsec → **deploy only when all are green** (OIDC, no stored AWS keys) → CodeDeploy blue/green with automatic rollback → smoke test.
Repository variables needed: `AWS_DEPLOY_ROLE_ARN`, `ALERT_EMAIL`, `AWS_REGION`.

## API summary

All endpoints need `Authorization: Bearer <Cognito access token>`; money-moving POSTs need `Idempotency-Key`; every response carries `X-Correlation-ID`. Amounts are **integer minor units** (ngwee).

| Endpoint | Who | Purpose |
|---|---|---|
| `POST /wallets` | any (agents/merchants need the group) | create a wallet |
| `GET /wallets/{id}`, `/wallets/{id}/ledger` | owner or admin | balance, hash-chained ledger |
| `POST /transfers` | wallet owner | wallet → wallet |
| `POST /merchant-payments` | customer | pay a merchant |
| `POST /agents/cash-in`, `/agents/cash-out` | agent / customer | float ↔ customer wallet |
| `POST /admin/float-topups` | admin | issue e-money to an agent's float |
| `GET /transactions/{id}` | parties or admin | status |
| `POST /transactions/{id}/reverse` | admin | compensating reversal |
| `POST /transactions/{id}/disputes`, `GET /disputes/{id}` | parties | raise / view dispute |
| `POST /disputes/{id}/resolve` | admin | `REVERSE` or `REJECT` |
| `GET /admin/integrity` | admin | run the reconciliation integrity checks now |
| `GET /health` | none | load balancer health |

Errors are `{"error": {"code", "message", "correlation_id"}}`. Business rejections are `422` with a stable `code` (`INSUFFICIENT_FUNDS`, `FRAUD_DECLINED`, …); `500 SAGA_FAILED_COMPENSATED` means the funds were returned and the client may retry with the **same** key.

## Layout

```
db/schema.sql              tables, append-only trigger, constraints, roles
src/momo/
  ledger/core.py           the only code that changes a balance (locking, hash chain)
  idempotency.py           Idempotency-Key protocol
  saga/                    steps, in-process orchestrator, Lambda handler, fault injection
  fraud/                   pure rules + service + Lambda handler
  reconciliation/          integrity checks, settlement, schedule handler
  events/outbox.py         transactional outbox -> SNS
  api/                     Flask app (stateless) + Cognito JWT verification
  common/                  db (IAM auth), JSON logging, tracing, metrics, money, hashing
statemachine/              Step Functions saga (ASL)
infra/terraform/           everything in AWS, as code
scripts/                   deploy, tokens, proofs, rollback + autoscale evidence, cost model
tests/                     unit + integration (real PostgreSQL)
load/                      k6 autoscaling scenario
docs/                      DESIGN, RUNBOOK, TEAM_PLAN
```

## Security reminders

Never commit `terraform.tfvars`, tokens, `.env` or tfstate (all git-ignored). Use a **sandbox** AWS account and the budget alert. Fault injection (`enable_fault_injection`) must be off outside the evidence run.
