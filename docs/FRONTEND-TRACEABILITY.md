# Mobile money frontend: feature and API traceability

## Repository audit

The application is a Flask/Python 3.12 API with a static HTML frontend. The public landing page is
served at `/`; sign-in and customer registration have separate pages; customer, agent, and
administrator workspaces use distinct page routes. The existing integrated operations console is
still served at `/demo` and `/console`. It is not a React application. The browser must use the
Flask API; it must never connect directly to PostgreSQL. Aurora stores wallets, transactions,
append-only ledger entries, fraud alerts, disputes, idempotency records, and settlement reports.
Amounts are integer minor units (100 ngwee per ZMW).

The money path uses a PostgreSQL transaction, wallet row locks in sorted order, a database
non-negative-balance constraint, an append-only ledger trigger, idempotency keys, and a
compensating saga. Cognito access JWTs are verified on each request. Backend ownership checks and
Cognito groups (`agents`, `merchants`, `admins`) remain authoritative.

## Feature-to-API mapping

| Frontend feature | API contract | Authorization and state boundary | Status |
|---|---|---|---|
| Sign in / sign out | `POST /auth/login/{customer,agent,admin}`, `POST /auth/logout`, `GET /auth/me` | The workspace uses the API-verified role; browser storage is not an authorization source and backend checks remain authoritative | Supported |
| Customer registration | `POST /auth/register` | Creates a customer account only; agent and administrator accounts remain operator-provisioned | Supported |
| Service availability | `GET /health` | Public, database connectivity check only | Supported |
| Wallet directory and detail | `GET /wallets`, `GET /wallets/{wallet_id}` | New list route returns caller-owned wallets; admins may list all | Implemented in this change |
| Wallet creation | `POST /wallets` | Backend permits customer wallets; merchant and agent creation requires their Cognito groups | Supported |
| Wallet activity | `GET /transactions?wallet_id={wallet_id}` | New list route returns only party-owned transactions unless caller is admin | Implemented in this change |
| Transfer | `POST /transfers` | Source-wallet ownership, allowed wallet kinds, and backend validation enforced; idempotency key required | Supported |
| Merchant payment | `POST /merchant-payments` | Caller-owned customer source; destination must be merchant wallet | Supported |
| Agent cash-in | `POST /agents/cash-in` | Requires `agents` group and agent-owned float source wallet | Supported |
| Customer cash-out | `POST /agents/cash-out` | Requires a customer-owned source wallet; this is not an agent-initiated operation | Supported by API and customer workspace |
| Profile editing | No profile update endpoint | Workspaces display only the identity returned by `/auth/me` | Unsupported |
| Agent float top-up | `POST /admin/float-topups` | `admins` group; system MINT wallet funds the agent | Supported |
| Wallet ledger | `GET /wallets/{wallet_id}/ledger` | Wallet owner or admin; records are read-only and hash chained | Supported |
| Journal detail | `GET /transactions/{transaction_id}/ledger` | Transaction party or admin; returns actual ledger rows for that transaction | Implemented in this change |
| Transaction detail and safe uncertain-result lookup | `GET /transactions/{transaction_id}`, `GET /operations?type=…&key=…` | Transaction party or admin; operation lookup is scoped to the authenticated caller and never resubmits | Implemented in this change |
| Reversal | `POST /transactions/{transaction_id}/reverse` | Admin only; backend rejects ineligible and already-reversed transactions; creates a compensating transaction | Supported |
| Disputes | `POST /transactions/{transaction_id}/disputes`, `GET /disputes/{dispute_id}`, `POST /disputes/{dispute_id}/resolve` | Parties may raise/read their dispute; admins resolve | Supported |
| Dispute directory | `GET /disputes` | New list route returns caller's disputes; admins may list all | Implemented in this change |
| Fraud alerts | `GET /admin/fraud-alerts` | Admin only; exposes only persisted alerts and rule explanations | Implemented in this change |
| Integrity and reconciliation | `GET /admin/integrity`; scheduled reconciliation Lambda persists reports | Admin-only live integrity check; report history is read-only | Supported |
| Settlement reports | `GET /admin/settlement-reports` | New admin-only read endpoint for reports already persisted by scheduled reconciliation | Implemented in this change |

## Explicitly unsupported by the current backend

There is no customer/account model or account directory, wallet freeze/suspension API, merchant or
agent directory separate from wallets, fraud-case review/resolution API, settlement calculation
or settlement-status API callable from the browser, evidence-upload flow, audit-log query, or
role/user administration API or profile editing endpoint. There is also no agent-initiated
cash-out flow: the existing cash-out API requires an authenticated customer as the source-wallet
owner. The role workspaces and console mark these capabilities unavailable instead of presenting
simulated actions or fabricated live metrics. Settlement history is available only after the
existing reconciliation job has persisted a report. Fraud review is read-only because the current
backend stores alerts but has no review lifecycle.

## API and implementation rules

* Money values passed to write endpoints are integer minor units. The UI converts a validated
  decimal amount to minor units; it never calculates or updates balances.
* A write is shown as successful only after the API returns a successful transaction response.
  A network timeout remains an unknown outcome; the workspaces and console look up the original
  idempotency key and block another payment while that result is unknown. They never retry
  automatically.
* Status and balance displays come from API responses. No mock balances, seeded records, or
  automatic demo fallback are used.
* The API list endpoints enforce ownership in SQL and cap page sizes. UI visibility is not an
  authorization boundary.
* No settlement or fraud result is inferred from arbitrary transaction rows.

## Run and verify the frontend

1. Follow the Python/PostgreSQL setup in the repository README and initialize the schema.
2. Start the API with the local database configured, or deploy the existing API.
3. Open the API root (`http://127.0.0.1:8080/`) for the landing page. Sign in through `/login` or
   register a customer through `/register`.
4. The login endpoint selects the role-specific authentication contract; the workspace then
   verifies the returned identity with `/auth/me`. Directly opening another role's page redirects
   according to the API-verified role, while every data/action endpoint remains protected by
   backend authorization.
5. To demonstrate payments, use wallets the authenticated identity is authorized to operate.
   Review the confirmation before submission. An uncertain result remains locked and can be looked
   up using the original idempotency key; do not make a new payment until the result is resolved.
6. `/demo` remains the integrated operations console for ledger, disputes, fraud-alert, and
   settlement views. A settlement report appears only after the scheduled reconciliation job has
   written one.

The static preview server (`.\run_demo_ui.bat`) does not proxy API calls and does not provide demo
data or credentials. For local use, configure the API process with
`CORS_ALLOWED_ORIGINS=http://127.0.0.1:5050`. Only explicitly listed origins are allowed; unset
means CORS disabled.

## Verification coverage

`tests/integration/test_api.py` covers ownership filtering, pagination validation, wallet balance
reads, transaction listing, read-only journal authorization, idempotency-key operation lookup,
dispute listing, and admin-only fraud/settlement access. Run it with the existing PostgreSQL
integration-test setup. The frontend uses native HTML forms and browser APIs; no frontend framework
or separate package manifest is introduced.
