# Correctness proofs

- Target: `local (in-process API + PostgreSQL)`
- Run: 2026-10-26T18:10:52.611587+00:00
- Overall: **PASS**

## 1. Idempotency (100 replays -> 1 debit)

| Result | Check | Detail |
|---|---|---|
| PASS | all 100 responses are 201 Created | status codes seen: [201] |
| PASS | all 100 responses refer to the SAME transaction id | 1 distinct id(s) |
| PASS | exactly 1 response was a real execution, 99 were replays | replays=99 |
| PASS | payer was debited exactly once | before=100000 after=85000 amount=15000 |
| PASS | ledger holds exactly ONE debit entry for the transaction | debit entries=1 |
| PASS | same key + different body is refused (422) | IDEMPOTENCY_KEY_REUSED |

## 2. Saga compensation (no money created/destroyed)

| Result | Check | Detail |
|---|---|---|
| PASS | crash at reserve.after    -> compensated, balances unchanged | http=500 outcome=SAGA_FAILED_COMPENSATED payer 185000->185000 payee 15000->15000 |
| PASS | crash at fraud.before     -> compensated, balances unchanged | http=500 outcome=SAGA_FAILED_COMPENSATED payer 185000->185000 payee 15000->15000 |
| PASS | crash at credit.before    -> compensated, balances unchanged | http=500 outcome=SAGA_FAILED_COMPENSATED payer 185000->185000 payee 15000->15000 |
| PASS | crash at credit.after     -> compensated, balances unchanged | http=500 outcome=SAGA_FAILED_COMPENSATED payer 185000->185000 payee 15000->15000 |
| PASS | crash at finalize.before  -> compensated, balances unchanged | http=500 outcome=SAGA_FAILED_COMPENSATED payer 185000->185000 payee 15000->15000 |
| PASS | global integrity after all faults: sum of balances = 0, ledger balanced, hash chains valid, escrow empty | {} |
| PASS | control: the same transfer without a fault completes | http=201 |

## 3. Concurrency safety (50 parallel, no overdraft)

| Result | Check | Detail |
|---|---|---|
| PASS | exactly 33 transfers succeeded (floor(1000/30)) | succeeded=33 |
| PASS | the other 17 were rejected with INSUFFICIENT_FUNDS | rejected=17 other=[] |
| PASS | payer balance is never negative | final=10 |
| PASS | payer balance == 1000 - 33*30 == 10 | final=10 |
| PASS | payee received exactly what the payer lost | payee +990 |
| PASS | every ledger balance_after is >= 0 (no overdraft at any point in history) |  |
| PASS | global integrity check passes | {} |
