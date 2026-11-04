# Frontend, API, and transaction verification report

**Verified:** 2026-11-04
**Environment:** disposable local PostgreSQL database and Flask API at `127.0.0.1:8080`; browser requests used test-only JWTs signed with a throwaway local secret.

## Evidence boundary

This report records local behavior only. No production/customer data, live Cognito session, AWS credentials, or deployed AWS resources were used. A successful local request does not establish live AWS availability or deployment state.

## Frontend screens exercised against the API

| Role | Screen or operation | Result |
|---|---|---|
| Customer | Dashboard, wallet balances, transfer, merchant payment, cash-out, and transaction history | Browser checks completed against the local API; transactions and balances came from API responses. |
| Agent | Agent dashboard and cash-in | Browser checks completed against the local API. Direct navigation to `/admin` redirected to the agent workspace. |
| Administrator | `/admin` dashboard and integrity check | API returned `200`; integrity status was `OK` with no reported issues. |
| Administrator | `/admin/transactions` | Six local seeded transactions were rendered from the API. |
| Administrator | `/admin/users` | The page explicitly reports that user management is unavailable because the API has no administrative user-directory or user-management endpoint. |
| Administrator | `/demo#risk` fraud alerts | The API-backed page rendered correctly and reported no persisted fraud alerts in this disposable database. |
| Administrator | `/demo#reconciliation` | Integrity checks rendered `OK`; no settlement reports were persisted in the local database. |
| Merchant | Dedicated workspace | Not available. Merchant operations are represented by merchant wallets and customer-initiated merchant payments; there is no dedicated merchant sign-in/workspace flow. |

Agent-initiated cash-out, profile editing, administrator user management, fraud-rule configuration/review actions, and settlement payout lifecycle are not implemented. The UI identifies unsupported functions rather than simulating them.

## Role authorization checks

Each role was checked with an independent test token. The following API checks returned these HTTP statuses:

| Request | Customer | Agent | Merchant | Admin |
|---|---:|---:|---:|---:|
| `GET /admin/integrity` | 403 | 403 | 403 | 200 |
| `GET /admin/fraud-alerts` | 403 | 403 | 403 | 200 |
| `POST /agents/cash-in` with an intentionally incomplete body | 403 | 404 | 403 | 403 |
| `POST /merchant-payments` with an intentionally incomplete body | 404 | 403 | 403 | 404 |

For the two POST rows, the expected-role request reached wallet lookup and returned `404` because the test body intentionally omitted wallet IDs; this checks the role gate, not a successful payment. Successful customer transfers, merchant payments, customer cash-out, and agent cash-in were exercised separately in the browser. An agent's direct request to `/admin/integrity` was also rejected with `403`.

These are API authorization checks, not a substitute for testing Cognito-backed identity in a deployed environment. UI visibility and redirects are not security boundaries; the API performs the authorization.

## Transaction correctness demonstrations

The local correctness proof run is recorded in [`docs/evidence/proof-results.md`](evidence/proof-results.md) and [`docs/evidence/proof-results.json`](evidence/proof-results.json).

| Demonstration | Result |
|---|---|
| Idempotency | 100 requests with one key returned one transaction and produced one debit; reusing the key with a different body was rejected. |
| Concurrency | Of 50 concurrent transfers of 30 units from a 1,000-unit balance, 33 succeeded and 17 were rejected for insufficient funds; the ending balance was 10 and never negative. |
| Saga compensation | Faults at all five configured saga points compensated; balances were unchanged after each fault and the control transfer completed. |
| Ledger integrity | The post-run integrity check passed with balanced entries, valid hash chains, and no escrow balance. |

These proofs use local PostgreSQL and the in-process API; they do not prove AWS Step Functions, SNS/SQS, or deployed database behavior.

## CI and AWS deployment status

The latest GitHub Actions run at the time of this report is [run 41850462656](https://github.com/Tensony/momo-platform/actions/runs/41850462656), which failed in the test job. The local fixes address the customer-group claims and transaction-history expectation in the integration test helper, plus the local proof runner's test-token and JWT-secret setup. The Windows workflow-equivalent run passed lint and all **147 tests** against disposable PostgreSQL; total coverage was **84.22%**, above the configured 60% threshold. The local correctness proofs also pass. The workflow has not been rerun remotely; the remote pipeline is therefore **not yet green**.

The Terraform configuration defines two-AZ networking, SNS/SQS with a dead-letter queue, ECS autoscaling, and CloudWatch alarms/dashboards. CodeDeploy rollback resources are optional and disabled by default. No live AWS inventory, deployment, scaling run, asynchronous delivery evidence, monitoring evidence, or rollback demonstration was collected. These remain configuration claims until verified in AWS.

The workflow deploys on a successful push to `main` when AWS credentials are available. Do not treat Terraform validation or local tests as proof of deployment. See [`ASSIGNMENT-TRACEABILITY.md`](ASSIGNMENT-TRACEABILITY.md) for the current CI and evidence boundary.
