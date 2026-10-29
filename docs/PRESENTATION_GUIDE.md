# Mobile Money Platform — Simple Presentation & Demo Guide

> **Goal:** An easy-to-read guide written in plain, simple English so every team member can present with confidence without memorizing heavy technical jargon.  
> **Structure:**  
>
> 1. Presentation Slides (~6 minutes: 1.5 minutes per person)  
> 2. Live Demos (~3–4 minutes: quick screen walkthrough)  
> 3. Cheat Sheet for Teacher Questions  

---

## Team Roles at a Glance

| Member | Presentation Topic | Live Demo to Show |
| --- | --- | --- |
| **Member 1** | What the project is & How our Cloud is organized | Demo 1 (Website is live) & Demo 2 (100 clicks test) |
| **Member 2** | How we stop people from stealing or losing money | Demo 3 (Server crash test) & Demo 4 (Account overdraft test) |
| **Member 3** | How we protect against hackers & Automated deployment | Demo 5 (Fake records test) & Demo 6 (GitHub green checkmarks) |
| **Member 4** | How we monitor errors & How we saved money on AWS | Demo 7 (Live Dashboard) & Demo 8 (Cost savings table) |

---

## Part 1: The Presentation (What Each Person Says)

---

### Member 1: Project Overview & Cloud Setup

#### Member 1 — What this means in plain words

* We built a mobile payment app (like MTN Mobile Money, Airtel Money, or PayPal).
* When people send money on weak mobile phone networks, buttons get tapped multiple times, connections cut off, and servers crash.
* Our main mission: **Never lose a customer's money, and never create money out of thin air.**

#### Slide 1: What We Built

* **What you say:**

  > *"Hello everyone. Today our team is presenting our Cloud Mobile Money Platform.
  >
  > Think of apps like Apple Pay or Mobile Money. People use them every day to send money to family or buy food.
  >
  > But what happens when your phone has poor internet, and you tap 'Pay' five times? Or what happens if the bank's power goes out halfway through?
  >
  > Our project guarantees three golden rules:
  > 1. You can tap the 'Send' button 100 times, but you will only be charged **once**.
  > 2. If the system crashes halfway through, your money automatically returns to your wallet.
  > 3. You can never spend more money than you actually have."*

#### Slide 2: How Our Cloud is Built

* **What you say:**

  > *"To keep the app running 24/7 without crashing, we host everything on Amazon Web Services (AWS):
  > * **Two Data Centers**: If one AWS building loses power, the other building immediately takes over.
  > * **Private and Safe**: Customers only see our secure front door. Our database and servers are locked inside a private network with no public internet access.
  > * **Instant Notifications**: The moment money moves, an automated background message notifies the fraud engine to check for suspicious activity.
  >
  > Now Member 2 will explain how we prevent money loss and overdrafts."*

---

### Member 2: Preventing Double-Spending & Crashes

#### Member 2 — What this means in plain words

* **Idempotency** means: If you press "Pay $10" five times, the system recognizes it's the exact same order and only charges you once.
* **Saga Pattern** means: Putting money into a temporary "holding box" (escrow) first. If the transfer finishes, give it to the receiver. If anything breaks, return it to the sender.
* **Concurrency** means: If you only have $10, and two people try to withdraw $10 at the exact same second, only one wins.

#### Slide 3: Stopping Double-Spending and Overdrafts

* **What you say:**

  > *"Thank you. When customers send money, two big problems can happen:
  >
  > First: **Double-Charging**.
  > If a customer taps 'Send' repeatedly because their screen froze, our system gives that request a unique digital stamp. The database processes it once, and ignores the other 99 duplicate taps.
  >
  > Second: **Going Below Zero**.
  > What if you have $10 in your account, and try to send $10 to two different friends at the exact same millisecond?
  > Our database locks your account row for a split second, serves the first person, sees you now have $0 left, and rejects the second person. Your balance can never go below zero."*

#### Slide 4: What Happens When the Server Crashes?

* **What you say:**

  > *"Imagine you send $50 to a friend. The system takes $50 from your account, but before your friend gets it, the server crashes. Where is your money?
  >
  > We use a 3-step safety process called a **Saga**:
  > 1. We move your money into a temporary holding box called **Escrow**.
  > 2. We check if your friend's account is valid and not fraudulent.
  > 3. We deliver the money to your friend.
  >
  > If the server crashes at any step, our system automatically runs a reverse refund: the holding box unlocks and gives your money right back to you.
  >
  > Now Member 3 will explain how we stop hacking and deploy our code."*

---

### Member 3: Security, Tamper Protection & Automation

#### Member 3 — What this means in plain words

* **Hash Chain** means: Like pages in a bank book where each page has a stamp from the previous page. If someone rips out or changes page 2, all pages after it become invalid.
* **CI/CD** means: An automated robot that checks our homework (tests our code) before letting it go live to real users.

#### Slide 5: Stopping Hackers & Fake Transactions

* **What you say:**

  > *"Thank you. In a bank, you have to worry about hackers or even dishonest employees trying to edit balances.
  >
  > We solved this with two major security shields:
  > 1. **No Passwords in Code**: We use temporary digital tokens. No secret passwords or bank keys are stored in our code files.
  > 2. **Tamper-Evident History**: Every transaction is cryptographically linked to the previous transaction, like links in a chain.
  >
  > If an attacker breaks into the database and changes their balance from $10 to $1,000, the digital chain breaks instantly, and our system rings an alarm. History cannot be secretly edited."*

#### Slide 6: Automated Testing & Deployment

* **What you say:**

  > *"We don't deploy updates by hand. We use an automated pipeline on GitHub:
  > * Every time we write code, an automated robot scans for secret leaks.
  > * It runs over **100 tests** checking every single financial calculation.
  > * If even a single test fails, the robot stops the deployment immediately.
  > * When all tests pass, it updates the live AWS cloud with zero downtime for users.
  >
  > Now Member 4 will explain how we monitor the live system and keep costs low."*

---

### Member 4: Live Monitoring & Cloud Cost Savings

#### Member 4 — What this means in plain words

* **Golden Signals** means: The dashboard shows 4 simple gauges (Speed, Traffic, Errors, and Server Energy).
* **Cost Optimization** means: We used smart AWS settings so we only pay for what we use, cutting our monthly bill by more than half.

#### Slide 7: Watching the Live System (Observability)

* **What you say:**

  > *"Thank you. You cannot protect what you cannot see.
  >
  > We built a live CloudWatch dashboard that tracks the health of our bank in real time:
  > * **Speed**: Making sure 95% of payments finish in less than 1 second.
  > * **Traffic**: Counting how many transactions happen per second.
  > * **Errors**: Flagging if anything fails so engineers get an email alert right away.
  > * **Tracing**: Every transaction has a tracking number, like a postal package. We can follow one single payment across every server in seconds."*

#### Slide 8: How We Cut AWS Costs

* **What you say:**

  > *"Running bank servers on AWS can get very expensive if you are not careful.
  >
  > We made four smart engineering decisions:
  > 1. We chose **Express Step Functions**, which cut workflow costs from $20 to **14 cents**.
  > 2. We used an elastic database that sleeps when nobody is using it, saving **$292 every month**.
  > 3. We simplified our cloud network, saving **$168 every month**.
  >
  > **The Big Result**: If our user base grows by **10 times**, our cost only goes up by **2 times**, because our servers expand and shrink automatically.
  >
  > Now we will show you the live running platform!"*

---

## Part 2: The Live Demonstration (Show Your Screen)

*One member shares their screen. Follow these 8 easy steps:*

---

### Step 1: Show the Live API Running

* **Member 1 speaks:**
* **Screen:** Open browser to: `https://momo-dev-alb-5850337.us-east-1.elb.amazonaws.com/health`
* **What you say:**

  > *"First, here is our live system running in the cloud right now. The status says `ok` with code 200, meaning our AWS cluster is healthy and ready for traffic."*

---

### Step 2: Show the 100 Duplicate Clicks Test

* **Member 1 speaks:**
* **Screen:** Open file `evidence-out/proof-results.md` (look at Section 1).
* **What you say:**

  > *"Here is proof #1. We sent 100 payments at the same time using the exact same request key. As you can see: exactly 1 payment was approved, 99 were recognized as duplicate replays, and the customer was only debited once."*

---

### Step 3: Show the Server Crash Test

* **Member 2 speaks:**
* **Screen:** Scroll down to Section 2 in `evidence-out/proof-results.md`.
* **What you say:**

  > *"Here is proof #2. We intentionally crashed the server at 5 different moments during a payment. Every time, the compensation kicked in, funds were returned to the sender, and the bank ledger remained completely balanced."*

---

### Step 4: Show the Overdraft Protection Test

* **Member 2 speaks:**
* **Screen:** Scroll down to Section 3 in `evidence-out/proof-results.md`.
* **What you say:**

  > *"Here is proof #3. A user with only $10 tried to make 50 transfers of 30 cents at once. Exactly 33 payments succeeded ($9.90 total), the other 17 were blocked for insufficient funds, and the balance ended at 10 cents. No overdraft occurred."*

---

### Step 5: Show the Digital Stamp / Hash Chain

* **Member 3 speaks:**
* **Screen:** Open file `db/schema.sql` (around line 85).
* **What you say:**

  > *"Here in our database code, you can see our security rules: no one is allowed to edit or delete history rows, and each row carries a cryptographic SHA-256 fingerprint of the previous row."*

---

### Step 6: Show the GitHub Robot (CI/CD)

* **Member 3 speaks:**
* **Screen:** Open your GitHub repo page (`Tensony/momo-platform/actions`).
* **What you say:**

  > *"Here is our GitHub Actions page showing green checkmarks. Every test, security scan, and build ran automatically before code was allowed onto AWS."*

---

### Step 7: Show the Health Dashboard

* **Member 4 speaks:**
* **Screen:** Open AWS CloudWatch Dashboard (`momo-dev-golden-signals`) or show `DESIGN.md` Section 7.
* **What you say:**

  > *"Here is our live CloudWatch monitoring dashboard showing real-time transaction speed, error rates, and server workload."*

---

### Step 8: Show the Cost Table

* **Member 4 speaks:**
* **Screen:** Open `docs/DESIGN.md` Section 10 (Table 10.0).
* **What you say:**

  > *"And finally, here is our cost table showing our monthly cost of $193, and how our smart serverless setup saves over $400 every month.
  >
  > That completes our presentation and demonstration. Thank you, and we welcome your questions!"*

---

## Part 3: Simple Answers to Teacher Questions

| What the Teacher Might Ask | How to Answer in Simple Words |
| --- | --- |
| **"Why didn't you use MongoDB or DynamoDB?"** | *"Because we are handling money. Relational databases like PostgreSQL give us strict row-level locking so two people can't spend the same dollar at the same time."* |
| **"What if your container crashes while someone is paying?"** | *"Our servers don't save state in memory. If a container dies, another container picks up the client's retry with the same key and either finishes the payment or returns the stored receipt."* |
| **"What is a Saga in one sentence?"** | *"A step-by-step transaction where if any middle step fails, the system automatically runs refunds backwards so nobody loses money."* |
| **"How did you keep AWS cheap?"** | *"We used serverless databases and Lambda functions that automatically pause and sleep when there is no customer traffic."* |
