# Mobile Money Platform — Team Presentation & Demo Guide

> **Target Audience:** Class & Lecturer  
> **Format:** Part 1: Slide Presentation (~6–7 mins) ➔ Part 2: Live Demos (~3–4 mins) ➔ Part 3: Q&A  
> **Team:** 4 Members (~2.5 minutes per member total)

---

## Quick Summary of Member Roles

| Member | Part 1: Presentation Topic | Part 2: Live Demo Item |
|---|---|---|
| **Member 1** | Problem Statement & Cloud Architecture | Demo 1 (Live Health Check) & Demo 2 (Idempotency Proof) |
| **Member 2** | Correctness Invariants & Saga Pattern | Demo 3 (Saga Crash Compensation) & Demo 4 (Concurrency Safety) |
| **Member 3** | Security, Hash Chains & CI/CD Pipeline | Demo 5 (Tamper-Evident Ledger) & Demo 6 (CI/CD Automated Gates) |
| **Member 4** | Observability, SLOs & Cost Economics | Demo 7 (Golden Signals Dashboard) & Demo 8 (Cost Model & 10× Scale) |

---

# PART 1: THE SLIDE PRESENTATION (Presentation Script)

---

### 🎙️ Member 1: Problem Statement & Cloud Architecture

#### **Slide 1: Problem & Invariants**
* **Spoken Lines:**
  > *"Good morning professor and class. Today our team presents our Cloud-Native Mobile Money Platform.
  >
  > In real-world financial apps, poor mobile networks, server crashes, and duplicate button clicks happen all the time. Our number one rule is: **Correctness under failure is non-negotiable.** Money cannot be created, destroyed, or double-spent.
  >
  > We built our entire platform around 3 strict invariants:
  > 1. **No double-spending**: Replaying a request 100 times debits the account exactly once.
  > 2. **Conservation of money**: Any failure mid-transaction must leave zero money created or destroyed.
  > 3. **Concurrency safety**: 50 simultaneous transfers can never overdraw an account balance."*

#### **Slide 2: Multi-AZ Microservice Architecture**
* **Spoken Lines:**
  > *"To ensure 99.5% availability, we designed a high-availability architecture across **2 AWS Availability Zones**:
  > - **Public Tier**: An Application Load Balancer with TLS termination.
  > - **Private Application Tier**: A stateless containerized `wallet-api` on AWS ECS Fargate, paired with three event-driven AWS Lambda microservices (`saga-steps`, `fraud-engine`, and `reconciliation`).
  > - **Private Data Tier**: Managed PostgreSQL on Aurora Serverless with Multi-AZ automated failover, completely isolated with no direct route to the public internet.
  > - **Asynchronous Outbox**: Outbox records are published to **Amazon SNS and SQS** with dead-letter queues, guaranteeing reliable asynchronous event delivery for fraud reviews."*

---

### 🎙️ Member 2: Correctness Invariants & Distributed Saga

#### **Slide 3: Idempotency & Concurrency Mechanics**
* **Spoken Lines:**
  > *"Thank you. How do we enforce these guarantees in code and database design?
  >
  > 1. **Idempotency**: Clients supply a unique `Idempotency-Key`. The database assigns a deterministic UUID and stores the request state. Duplicate requests receive the cached successful response instantly without touching the ledger again.
  > 2. **Concurrency Protection**: When multiple transfers hit the same wallet simultaneously, we take PostgreSQL row-level locks (`SELECT ... FOR UPDATE`) sorted by wallet ID to prevent deadlocks. The balance check happens **after** the lock is held. A database `CHECK (balance >= 0)` constraint serves as the final mathematical backstop."*

#### **Slide 4: The Distributed Saga Pattern**
* **Spoken Lines:**
  > *"When money moves across distributed microservices, standard database transactions don't work. Instead, we use an **orchestrated Saga**:
  > - **Forward Flow**: Reserve funds into an ESCROW account → run fraud evaluation → credit the recipient → finalize the transaction.
  > - **Automated Compensation**: If any step fails or times out, the saga automatically executes reverse compensating entries: releasing funds back from ESCROW to the sender. The balance sheet `SUM(balances) = 0` is strictly conserved at all times."*

---

### 🎙️ Member 3: Security & CI/CD Engineering

#### **Slide 5: Security & Cryptographic Ledger Integrity**
* **Spoken Lines:**
  > *"Thank you. Security is embedded into every layer of our platform:
  > - **Zero Hardcoded Secrets**: Secrets Manager manages database credentials, IAM database authentication handles service logins, and `gitleaks` scans full git history on every commit.
  > - **Tamper-Evident Ledger**: We implemented a **SHA-256 cryptographic hash chain** where every ledger row hashes the previous entry. Furthermore, database triggers reject any `UPDATE`, `DELETE`, or `TRUNCATE` actions. Even if a database administrator tampers with a row, our reconciliation audit immediately flags the broken chain."*

#### **Slide 6: Automated CI/CD Pipeline**
* **Spoken Lines:**
  > *"All 120+ cloud resources are 100% managed as Code using Terraform. Our GitHub Actions pipeline enforces automated quality gates before any deployment:
  > 1. Secret detection using Gitleaks.
  > 2. Code linting with Ruff.
  > 3. 113 unit and integration tests run against a real PostgreSQL container with a 60% coverage gate (we achieved 92%).
  > 4. Automated correctness proofs and Terraform schema validation.
  > 5. Zero-downtime rolling deployment to Amazon ECR and ECS Fargate with automated `/health` smoke tests."*

---

### 🎙️ Member 4: Observability, SLOs & Cost Modeling

#### **Slide 7: Observability & Golden Signals**
* **Spoken Lines:**
  > *"Thank you. You cannot manage what you cannot measure. Our observability follows Google's **Four Golden Signals**:
  > - **Latency**: Tracking p50, p95, and p99 response times against our SLO threshold ($p95 < 1\text{s}$).
  > - **Traffic**: Transactions per second and active sagas.
  > - **Errors**: Distinguishing client validation rejections from technical 5xx errors ($99.5\%$ availability SLO).
  > - **Saturation**: Monitoring CPU, memory, and database connections.
  >
  > Every request carries an `X-Correlation-ID` traced across containers, Lambdas, and DB queries using AWS X-Ray."*

#### **Slide 8: Cost Model & 10× Scaling Economics**
* **Spoken Lines:**
  > *"Finally, we conducted a cloud economics analysis using our cost model script (`cost_model.py`):
  > 
  > We engineered four key optimizations:
  > 1. **Step Functions Express**: Slashed orchestration costs from $\$20.00$ to **$\$0.14$** per 100k transactions.
  > 2. **Pragmatic Networking**: A single NAT gateway instead of 9 interface endpoints saves **$\$167.90/month**.
  > 3. **Aurora Serverless v2**: Replaced over-provisioned instances with elastic ACUs, saving **$\$292.00/month**.
  > 4. **Log Retention**: Capped at 14 days to prevent CloudWatch accumulation.
  >
  > **The Key Takeaway**: When business traffic scales by **10×**, our total cost only increases by **$2.3\times$** because compute scales elastically while fixed costs are amortized."*

---
---

# PART 2: THE LIVE DEMOS (Screen Walkthrough)

Transition to screen share:
> *"Now we will walk you through the live demonstration of the running system across 8 verified proofs."*

---

### 🔹 Demo 1: Architecture & Live ALB Health
* **Presented by:** Member 1
* **On Screen:** Open browser to the live ALB health URL:
  ```text
  https://momo-dev-alb-5850337.us-east-1.elb.amazonaws.com/health
  ```
* **What to say:**
  > *"Here is our live AWS environment. Calling the load balancer `/health` check returns `200 OK` `{"status":"ok"}` from our running ECS Fargate cluster."*

---

### 🔹 Demo 2: Idempotency Proof (100 Replays)
* **Presented by:** Member 1
* **On Screen:** Open `evidence-out/proof-results.md` (Section 1).
* **What to say:**
  > *"Here are the verified results of our automated idempotency proof: 100 concurrent requests were sent with the same key. Exactly 1 executed, 99 were identified as duplicate replays, and the sender was debited only once."*

---

### 🔹 Demo 3: Saga Crash Compensation
* **Presented by:** Member 2
* **On Screen:** Open `evidence-out/proof-results.md` (Section 2).
* **What to say:**
  > *"In proof #2, we injected crashes at 5 different points in the transfer lifecycle. In all 5 cases, the saga triggered compensation, zero money was created or destroyed, ESCROW returned to 0, and the ledger balanced."*

---

### 🔹 Demo 4: Concurrency Safety (50 Parallel Requests)
* **Presented by:** Member 2
* **On Screen:** Open `evidence-out/proof-results.md` (Section 3).
* **What to say:**
  > *"In proof #3, a user with $10 attempted 50 simultaneous $0.30 transfers. Thanks to row-level locking, exactly 33 succeeded ($9.90), 17 failed with `INSUFFICIENT_FUNDS`, and the final balance remained positive at $0.10."*

---

### 🔹 Demo 5: Tamper-Evident Hash Chain Audit
* **Presented by:** Member 3
* **On Screen:** Open `db/schema.sql` (lines 80–100).
* **What to say:**
  > *"Here in the database schema, we show the append-only triggers and the SHA-256 hash chaining. If any historical row is altered, the chain breaks and hourly reconciliation alerts the operations team."*

---

### 🔹 Demo 6: CI/CD Pipeline & Automated Gates
* **Presented by:** Member 3
* **On Screen:** Open GitHub Actions tab on `Tensony/momo-platform`.
* **What to say:**
  > *"Here is our live GitHub Actions pipeline. On every commit, secret scanning, test coverage gates, and Terraform verification run automatically before triggering our zero-downtime container rollout."*

---

### 🔹 Demo 7: Observability Dashboard & Tracing
* **Presented by:** Member 4
* **On Screen:** Open AWS Console ➔ CloudWatch ➔ Dashboards ➔ `momo-dev-golden-signals` (or `docs/DESIGN.md` Section 7).
* **What to say:**
  > *"Here is our CloudWatch dashboard tracking the 4 Golden Signals: p95 latency against our SLO target, request traffic, error percentages, and ECS/Aurora saturation."*

---

### 🔹 Demo 8: Cost Model & Scaling Summary
* **Presented by:** Member 4
* **On Screen:** Open `docs/DESIGN.md` Section 10 (Table 10.0).
* **What to say:**
  > *"Finally, here is our cost model showing our baseline of $193/month and the 10× scaling projection of $439/month, saving hundreds of dollars through Serverless v2 and Step Functions Express.
  >
  > This concludes our presentation and live demonstration. Thank you!"*

---

# PART 3: ANTICIPATED Q&A CHEATSHEET 💡

| Question from Lecturer / Class | Ideal Answer |
|---|---|
| **Why PostgreSQL instead of DynamoDB?** | *"Moving money requires multi-row atomic transactions, row-level locking (`FOR UPDATE`) to prevent concurrent overdrafts, and set-based SQL aggregation for reconciliation. DynamoDB would force complex locking logic into application code."* |
| **What happens if an ECS container dies mid-request?** | *"The API is completely stateless. The client's retry with the same `Idempotency-Key` is picked up by another container; the transaction lease either resumes or returns the already-committed result from the database."* |
| **How is the ledger protected from database administrators?** | *"PostgreSQL triggers reject updates and deletes, permissions are revoked, and every entry is chained with SHA-256 hashes. Any modification invalidates the chain downstream."* |
| **Why Express Step Functions instead of Standard?** | *"Standard Step Functions bill per state transition, which would cost $200/month at 1M transactions. Express Step Functions bill per execution duration, costing only $1.42 for the same volume (over 99% savings)."* |
| **Why is the database in private subnets?** | *"Financial databases should never be directly accessible from the internet. Only authorized containers inside the VPC app tier can connect, reducing the attack surface to zero external IP exposure."* |
