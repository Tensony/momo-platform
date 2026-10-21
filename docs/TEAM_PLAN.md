# Team plan — 3 people, deadline Thursday 22 Oct

The code, tests, Terraform, CI/CD, scripts and documents exist. What remains is **work only your team can do in your own AWS account**, learning the system well enough to defend it, and producing evidence. Each person owns one area **and commits their own work under their own name** — the brief requires a meaningful history from all members, and that has to be real.

## Ownership (who commits what)

| Person | Area | Files | Must be able to explain |
|---|---|---|---|
| **A — Data & correctness** | ledger, idempotency, saga, proofs | `db/schema.sql`, `src/momo/ledger`, `idempotency.py`, `saga/`, `statemachine/`, `tests/integration`, `scripts/proofs` | why `FOR UPDATE` in sorted order prevents overdraft *and* deadlock; the 5 fault points; what the hash chain does and does not protect |
| **B — Platform & delivery** | Terraform, deploy, CI/CD, rollback, scaling | `infra/terraform`, `Dockerfile`, `scripts/*.sh`, `.github/workflows` | the 3 network tiers; blue/green + alarms-gated rollback; IAM table in DESIGN §5.1 |
| **C — API, security, observability, cost** | API, auth, fraud, dashboard, SLOs, cost | `src/momo/api`, `fraud/`, `reconciliation/`, `observability.tf`, `docs/`, `scripts/cost_model.py` | JWT validation checklist; golden signals; every alarm's response; the 4 cost optimisations + 10× projection |

Suggested first step: one of you creates the GitHub repo and pushes the baseline; then **each person makes their own commits** as they review, change and extend their area (e.g. A tunes saga retries, B parameterises the environment, C adds a fraud rule + test). Small, real, descriptive commits.

## Schedule

### Tuesday (today) — get it running, learn it
- [ ] All: clone, `make install`, run the tests locally against Postgres (`docker run -p 5432:5432 -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=momo_test postgres:16`, then `make cover`, `make proofs`). Watch the three proofs pass.
- [ ] **B:** AWS account ready (use a fresh/sandbox account; set a budget alert). Install Terraform, Docker, AWS CLI. Run `terraform -chdir=infra/terraform init && validate` and **fix any provider-version errors** (the template was checked offline only — see DESIGN §12.4).
- [ ] **A:** read `ledger/core.py`, `idempotency.py`, `saga/steps.py`; write 1–2 extra tests of your own.
- [ ] **C:** read `api/app.py`, `api/auth.py`, `fraud/rules.py`; add one fraud rule with tests.
- [ ] Push to GitHub; enable Actions; confirm CI is green (add repo variables `AWS_DEPLOY_ROLE_ARN`, `ALERT_EMAIL`, `AWS_REGION`; OIDC role per DESIGN §6).

### Wednesday — deploy and capture evidence (this is the long pole: allow for failed first applies)
- [ ] **B:** `scripts/deploy.sh <email>` (≈ 20–30 min: Aurora is slow). Confirm the SNS e-mail. Fix issues, re-apply.
- [ ] **A (after deploy):** `scripts/get_tokens.sh`, then enable fault injection (`-var enable_fault_injection=true`) and relax fraud (`velocity_max_txns` = 100000), run `scripts/proofs/run_proofs.py --base-url <api_url> …` → saves `evidence-out/proof-results.md`. **Turn fault injection back off afterwards.**
- [ ] **B:** `scripts/rollback_demo.sh` → `evidence-out/rollback-deployment.json`; screenshot the CodeDeploy rollback.
- [ ] **B + C:** run `k6` (`load/k6-autoscale.js`) and `scripts/autoscale_evidence.sh`; screenshot ECS task count and the dashboard during the ramp.
- [ ] **C:** screenshot the dashboard (all four golden-signal rows), the alarm list, a Logs Insights query by correlation id, and an X-Ray trace map. Trigger one alarm deliberately (e.g. stop the DLQ consumer) and show the e-mail.
- [ ] **C:** CI evidence: screenshot a green pipeline, and a *red* one (push a throwaway commit containing a fake AWS key `AKIA…` on a branch → gitleaks fails → revert) to show the scan gates.
- [ ] Commit evidence under `docs/evidence/` (screenshots + JSON). Never commit tokens or `terraform.tfvars`.

### Thursday — finish and rehearse
- [ ] **C:** Cost Explorer (spend lags up to ~24 h; if empty, use the Billing → Bills page) → fill the "Actual" column in DESIGN §10.0 and keep the estimate beside it with the note on differences.
- [ ] **B:** `terraform destroy` **only after** all evidence is captured and the marker's demo is done (or keep running if they will inspect live) — mind the budget.
- [ ] All: read DESIGN end-to-end; each person rehearses explaining their area in 3 minutes; agree who demos what.
- [ ] Final check against DESIGN §11 (traceability) — every gate has a link and evidence.

## Demo script (10 minutes)
1. Architecture diagram (DESIGN §2) — 2 AZs, 3+ services, async path, private subnets.
2. **Idempotency** — run proof §1: 100 replays, one debit. Show the `UNIQUE` constraint as the last line of defence.
3. **Saga** — run with `credit.after` fault: money returns, ESCROW empty, reconciliation clean.
4. **Concurrency** — 50 parallel, 33 succeed, balance 10.
5. **Tamper test** — run locally: `pytest -k tampering` edits a ledger row with triggers disabled (as a DB superuser could) and the hash-chain verifier flags it. (Aurora is private, so this is shown locally, not against AWS.)
6. **Rollback** — the recorded deployment, or live `rollback_demo.sh`.
7. **Observability** — dashboard, one trace by correlation id, alarm e-mail, SLOs.
8. **Cost** — table, the 4 optimisations, the 10× projection.
