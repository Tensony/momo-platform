# My Presentation Script (Slides 14–18)

---

## Slide 14: Security Controls (Pass/Fail Gates)
* **What this slide is about:** How we lock down passwords, data, and user logins.
* **What to say:**
  > "Thank you. Starting on Slide 14, let’s look at how we secure the platform across six strict layers:
  > 1. **Zero Passwords in Code:** Our GitHub pipeline scans our entire commit history with `gitleaks`—if anyone ever types a password in code, the build fails immediately. Instead, our services connect to the database using temporary IAM tokens.
  > 2. **Secrets Management:** Master passwords rotate automatically in AWS Secrets Manager, and fraud limits live in SSM Parameter Store.
  > 3. **Least-Privilege IAM:** Every service gets its own separate badge with only the exact permissions it needs.
  > 4. **Encryption Everywhere:** All data is encrypted at rest using KMS keys, and all traffic requires TLS 1.3.
  > 5. **Private Network:** Our API tasks, Lambdas, and Aurora database sit inside private subnets with no direct route from the public internet.
  > 6. **Strong Login (AuthN/AuthZ):** Users sign in through Amazon Cognito with JWT tokens, and we verify wallet ownership on every single request."

---

## Slide 15: Least Privilege & Network Isolation
* **What this slide is about:** Proving that even if one service is compromised, it cannot touch the rest of the bank.
* **What to say:**
  > "On Slide 15, this table shows our **Least Privilege** rule in action:
  > - Each service can only do its own job. For example, the **ECS API** can start a payment workflow, but it **cannot** write settlement reports to S3. And the **Reconciliation** service can write daily reports, but it **cannot** start a payment.
  > - Even inside the database, our application role (`momo_app`) only has permission to `SELECT` and `INSERT` on the ledger—it is physically blocked from updating or deleting past records.
  > - For our network, we split our VPC across **2 Availability Zones** into Public, Private App, and Private Data subnets. And to save over $160 a month, we made a deliberate, documented trade-off to use a single NAT Gateway."

---

## Slide 16: Infrastructure as Code & CI/CD
* **What this slide is about:** How we build the cloud automatically with Terraform and test everything on GitHub before deploying.
* **What to say:**
  > "Slide 16 shows our **Engineering & CI/CD pipeline**:
  > - First, **100% Infrastructure as Code**: All 127 AWS resources are built automatically using Terraform—nothing is clicked by hand in the AWS console.
  > - Second, look at the **5 green pipeline gates** at the top: every time we push code, GitHub Actions runs `gitleaks` for secrets, `Ruff` for code quality, **over 113 automated tests** with 92% business-logic coverage, our **3 live correctness proofs**, and `tfsec` to scan our Terraform security.
  > - If even one check fails, the deployment stops.
  > - When we deploy to AWS, we use **Blue/Green deployments** through CodeDeploy—if a new version triggers an alarm, AWS automatically rolls back to the previous working version with zero downtime."

---

## Slide 17: Observability — Golden Signals, Tracing & Alarms
* **What this slide is about:** How we see inside the live system and trace any customer's payment in seconds.
* **What to say:**
  > "Moving to Slide 17, you cannot protect a bank if you cannot see what is happening inside it:
  > - **Structured Logs:** Every log line is printed in JSON with a unique tracking number (`correlation_id`). We can follow one customer's payment from the API, through the Saga, all the way to the background SQS fraud queue.
  > - **Distributed Tracing:** AWS X-Ray maps the exact millisecond spent in Flask, Step Functions, Lambda, and PostgreSQL.
  > - **Live Dashboard & 8 Alarms:** Our CloudWatch dashboard tracks the 4 Golden Signals—**Latency, Traffic, Errors, and Saturation**—backed by 8 automated alarms that alert engineers and link directly to our incident Runbook."

---

## Slide 18: SLIs & SLOs — Core Journey: Send Money
* **What this slide is about:** The concrete promises (Service Level Objectives) we make for speed, uptime, and accuracy.
* **What to say:**
  > "Finally, on Slide 18, we define our strict performance targets—our **SLIs and SLOs**—for sending money:
  > 1. **Availability (99.5%):** 99.5% of requests must succeed without server errors.
  > 2. **Speed / Latency (< 1 second):** 95% of transfers finish in under 1 second.
  > 3. **Financial Correctness (100%):** Look at line 3—our error budget for correctness is **ZERO**. If our server crashes mid-payment and safely refunds the user, that counts against *Availability*, never *Correctness*. We never lose or invent a single cent.
  > 4. **Async Fraud Speed (99.9% < 60s):** Background fraud checks finish in under 60 seconds.
  >
  > Every single one of these targets is tied to a live CloudWatch alarm. Now I will hand it over to the next speaker to cover Auto-Scaling and Cost."

---

## Quick 10-Second Memory Cheat Sheet
* **Slide 14 (6 Security Shields):** No secrets in Git, Secrets Manager, IAM roles, KMS + TLS 1.3 encryption, Private subnets, Cognito login.
* **Slide 15 (Who Can Do What):** Each service only does its own job; DB app role can only `INSERT` (never edit/delete); 2 AZs + 1 NAT Gateway to save money.
* **Slide 16 (Terraform + GitHub Robot):** 127 Terraform resources, 5 CI gates (secrets, lint, 113+ tests, 3 proofs, tfsec), Blue/Green auto-rollback.
* **Slide 17 (Watching the System):** `correlation_id` tracks every payment, X-Ray tracing, 4 Golden Signals dashboard, 8 CloudWatch alarms.
* **Slide 18 (Our 4 Promises):** 99.5% uptime, under 1 second speed, **100% money accuracy (zero error budget!)**, under 60s fraud check.

