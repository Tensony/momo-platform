# Demo Commands

## Run the full local application

Follow the Windows PostgreSQL/API setup in [README.md](README.md), then start the frontend proxy
in a second terminal:

```powershell
$env:MOMO_API_BASE_URL = "http://127.0.0.1:8080"
.\run_demo_ui.bat
```

Use the API-backed pages at `http://127.0.0.1:5050`. Opening `demo\index.html` directly is only a
static preview and does not provide API authentication or transactions.

## Demonstrate each role

```powershell
# Register a customer from /register in the browser, then provision the operator-managed roles.
.\.venv\Scripts\python.exe scripts\provision_user.py agent --identifier AGENT-007 --full-name "Local Agent"
.\.venv\Scripts\python.exe scripts\provision_user.py merchant --identifier MERCHANT-001 --full-name "Local Merchant"
.\.venv\Scripts\python.exe scripts\provision_user.py admin --identifier admin@momo.platform --full-name "Local Admin"
```

Provisioning prompts for each password. Agent and merchant wallets are created automatically.
Sign in at `/login/customer`, `/login/agent`, `/login/merchant`, or `/login/admin`.

| Role | Demonstrate | Not permitted |
|---|---|---|
| Customer | Register; review own wallet and transactions; transfer to a wallet; pay a merchant; cash out through an agent; raise a dispute on a transaction party to the customer | Agent cash-in, float top-up, admin review or reversal |
| Agent | Review own float and transactions; cash-in from own agent float to a customer wallet | Customer cash-out as an agent; merchant payment; admin operations |
| Merchant | Review own merchant wallet, incoming payments, transactions and ledger; make a permitted transfer; raise a dispute on a transaction party to the merchant | Customer-initiated merchant-payment endpoint, agent cash-in, admin operations |
| Administrator | Review platform wallets and transactions; issue agent float; run integrity checks; review persisted fraud/settlement records; reverse eligible transactions and resolve disputes | Privileged user accounts are operator-provisioned, not self-registered through the app |

For an end-to-end money flow, sign in as admin and top up the provisioned agent float, then sign in
as the agent and cash in to the registered customer wallet. Sign in as the customer to transfer to
the merchant wallet (or pay it), then sign in as merchant to verify the received transaction and
ledger. Every action is checked by the API; a hidden/visible UI control is not an authorization
boundary. Do not retry a payment after an uncertain result—look it up using the original idempotency
key.

## Run automated tests

```powershell
.\make.bat test
```

The full suite needs PostgreSQL; see the README for the disposable local database setup.

## Run correctness proofs (PostgreSQL required)

```powershell
.\make.bat proofs
```

## Run tamper-evident ledger and append-only tests

```powershell
.\make.bat test -k "append_only or hash_chain"
```

## Run the AWS cloud cost optimization model

```powershell
.\.venv\Scripts\python.exe scripts\cost_model.py
```
