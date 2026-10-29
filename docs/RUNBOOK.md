# Runbook

Every alarm notifies the `*-alerts` SNS topic (e-mail). Each playbook: **detect → contain → fix → verify**.
Find any request across all services with its correlation id (returned in every response as `X-Correlation-ID` and in every error body):

```text
# CloudWatch Logs Insights - select log groups: /momo/<env>/wallet-api, /aws/lambda/momo-<env>-*
fields @timestamp, service, level, msg, txn_id, action
| filter correlation_id = "PASTE-ID-HERE"
| sort @timestamp asc
```

Open the same trace in **X-Ray → Traces** by `trace_id` to see API → Step Functions → Lambdas → database timings.

## P1 — Money is at risk (page immediately)

### Alarm `saga-compensation-failed` (Step Functions `ExecutionsFailed`)

*A saga could not compensate after 5 retries. Funds are parked in ESCROW (money is conserved, but a customer is out of pocket).*

1. **Detect:** Logs Insights, filter `msg = "saga step failed - compensating"` or `state = "COMPENSATION_FAILED"`:
   `SELECT txn_id, state, last_error FROM sagas WHERE state = 'COMPENSATION_FAILED';`
2. **Contain:** nothing else is affected; other transactions proceed.
3. **Fix:** cause is usually DB unavailability or a frozen/closed wallet. Resolve the cause, then re-run compensation — it is safe to repeat because it is state-driven and idempotent:
   invoke `momo-<env>-saga-steps` with `{"action":"reverse_credit","ctx":{…from the transactions row…}}` then `{"action":"release", …}`, then `{"action":"mark_failed","error":"manual compensation", …}`.
4. **Verify:** next reconciliation shows `status: OK`; `SELECT balance FROM wallets WHERE id = '00000000-0000-0000-0000-000000000002'` (ESCROW) returns to the sum of genuinely in-flight transactions.

### Alarm `reconciliation-discrepancy`

*The books don't balance, or a hash chain is broken.*

1. **Detect:** `SELECT summary->'issues' FROM settlement_reports ORDER BY created_at DESC LIMIT 1;` — keys tell you which check failed:
   `conservation_sum_of_balances`, `ledger_net`, `unbalanced_steps`, `wallet_ledger_drift`, `broken_hash_chains`, `stuck_sagas`, `escrow_mismatch`.
2. **Contain:** for `broken_hash_chains` or `wallet_ledger_drift` treat as possible tampering: freeze the named wallets (`UPDATE wallets SET status='FROZEN' …` — as the DB admin; the app role cannot touch the ledger) and review **CloudTrail** for who accessed Aurora/Secrets Manager.
3. **Fix:** a ledger is never edited. Correct with a new compensating transaction after the cause is understood. Restore from the point-in-time backup if corruption is confirmed.
4. **Verify:** invoke the reconciliation Lambda with `{"task":"reconcile"}`; expect `"status":"OK"`.

## P2 — Service degraded

### Alarm `api-5xx-rate`

1. **Detect:** dashboard *Errors*; Logs Insights `filter level = "ERROR"`; did a deployment just happen (CodeDeploy console)?
2. **Contain:** if during a deployment, CodeDeploy rolls back automatically (the alarm gates it). If after, roll back by redeploying the previous task definition: `aws deploy create-deployment …` with the prior `TaskDefinition`.
3. **Fix:** common causes — Aurora failover in progress (≈30 s, self-heals), saga Lambda throttled/erroring (check `AWS/Lambda Errors`), expired config. Compensated failures return `500 SAGA_FAILED_COMPENSATED` — money is safe; clients retry with the *same* Idempotency-Key.
4. **Verify:** 5xx % back under 2 %; no `COMPENSATION_FAILED` rows.

### Alarm `api-latency-p95`

1. **Detect:** dashboard *Latency* + *Saturation*. Is Aurora `ACUUtilization` high? Are tasks at max (`RunningTaskCount` = 6)? Slow queries: RDS **Performance Insights**.
2. **Fix:** raise `api_max_tasks` / `aurora_max_acu` and apply Terraform; hot-wallet contention (many payments to one merchant) shows as lock waits — expected serialisation, scale the merchant's settlement model rather than the DB.
3. **Verify:** p95 < 1 s for 15 minutes.

### Alarm `unhealthy-hosts`

1. **Detect:** ECS service events; task stop reason (`aws ecs describe-tasks`). `/health` returns 503 `db_unreachable` if the DB connection fails.
2. **Fix:** DB reachability (security group `db` ← `api`), image pull failure, OOM (raise `api_memory`). ECS replaces unhealthy tasks automatically.
3. **Verify:** `UnHealthyHostCount` back to 0.

### Alarm `fraud-dlq-not-empty`

1. **Detect:** SQS console → DLQ → *Poll for messages*; each message is a `TransactionCompleted` event that failed 5 times. Correlate by `correlation_id`.
2. **Fix:** fix the consumer bug / DB issue, then **SQS → Start DLQ redrive** back to the source queue. Safe: the consumer is idempotent (`UNIQUE (txn_id, decision)`).
3. **Verify:** DLQ depth 0; `fraud_alerts` populated.

### Alarm `aurora-saturation`

Raise `aurora_max_acu`; inspect Performance Insights for the top SQL; check connection count (`DatabaseConnections`) — beyond ~10 API tasks add **RDS Proxy**.

### Alarm `unhandled-exceptions`

A code path raised something not mapped to a clean error. Find by `correlation_id` from the Logs Insights query (`level = "ERROR"`), fix, redeploy. Customers got a generic 500 — verify no money moved (saga is transactional; run reconciliation).

## Routine operations

| Task | How |
| --- | --- |
| Tune fraud thresholds | `aws ssm put-parameter --name /momo/fraud/config --type String --overwrite --value '{"velocity_max_txns": 8}'` (60 s cache; no deploy) |
| Release a new version | merge to `main` → pipeline; or `scripts/deploy.sh` |
| Roll back | automatic during deployment; otherwise redeploy the previous task definition via CodeDeploy |
| Run reconciliation now | invoke `momo-<env>-reconciliation` with `{"task":"reconcile"}` |
| Publish stuck outbox events | invoke it with `{"task":"outbox_sweep"}` |
| Rotate DB credentials | managed by RDS (master secret); app uses IAM auth, nothing to rotate |
| Onboard an agent/merchant | add user to Cognito group `agents`/`merchants`; admin tops up agent float via `POST /admin/float-topups` |
| Take the system down safely | `terraform destroy` (with `deletion_protection=false`); S3 buckets are emptied by `force_destroy` |
