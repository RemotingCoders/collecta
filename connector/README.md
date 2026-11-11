# Collecta PayPal Sandbox connector

A Power Platform custom connector for the **PayPal REST API (sandbox)**. It is the payments tool belt of
**Collecta**, an AI collections agent for Dynamics 365 and Power Platform: the agent uses it to invoice
customers, chase unpaid invoices, collect one-time payments, send payouts and reconcile what was paid.

It works from Power Automate, Power Apps and Copilot Studio. Every operation, parameter and property has a
summary and a description, so a Copilot Studio agent can choose the right tool and the Power Automate designer
shows typed dynamic content.

> **Sandbox only.** The connector talks to `api-m.sandbox.paypal.com`. No real money moves.

## Files

| File | Purpose |
| --- | --- |
| `apiDefinition.swagger.json` | OpenAPI 2.0 (Swagger) definition: 14 operations with request and response schemas. |
| `apiProperties.json` | Connection parameters (Client ID / Secret), brand color, and the operations that run the custom code. |
| `script.csx` | Custom code that turns the connection's Client ID / Secret into a PayPal OAuth bearer token. |
| `icon.png` | Connector icon (144 x 144 PNG), the same one the Collecta solution carries. |

## How authentication works

PayPal's REST API needs an OAuth 2.0 access token obtained with the *client credentials* grant, which the
custom connector security settings cannot do on their own. So:

1. The connection uses **Basic authentication**: username = PayPal **Client ID**, password = PayPal **Secret**.
2. On every call, `script.csx` takes the incoming `Authorization: Basic ...` header and posts it to
   `https://api-m.sandbox.paypal.com/v1/oauth2/token` with `grant_type=client_credentials`.
3. It replaces the header with `Authorization: Bearer {access_token}` and forwards the original request.
4. The PayPal response is returned unchanged, including errors (a wrong Client ID or Secret surfaces as
   PayPal's own `401 invalid_client`).

The script also does two small things PayPal requires:

- adds `Prefer: return=representation` to **Create draft invoice**, **Create order** and **Capture order**, so
  the response contains the full resource instead of a bare link;
- sends `{}` with a JSON content type when a `POST` has no body (send, remind, cancel, capture, generate
  number), because PayPal rejects such calls without a content type.

No credentials are stored in this repository or in the script. They live only in the connection.

## Prerequisites

1. A PayPal developer account: <https://developer.paypal.com>.
2. A **sandbox REST app**: Developer Dashboard > **Apps & Credentials** > **Sandbox** > **Create App**
   (type *Merchant*). Copy its **Client ID** and **Secret**.
3. In the app's **Features**, enable what you plan to use: **Invoicing**, **Payouts** and
   **Transaction Search**. A call to a feature that is not enabled returns `403`.
   A feature change can take a while to apply; PayPal may keep answering `403` until it issues a new access
   token for the app.
4. At least one sandbox **Personal** account (Dashboard > **Testing Tools** > **Sandbox Accounts**) to act as
   the customer who receives invoices and approves payments.

## Create the connector

### Option A: paconn CLI

```bash
pip install paconn
paconn login

# Optional: check the definition against the connector rules
paconn validate --api-def connector/apiDefinition.swagger.json

# Create the connector (you are prompted for the environment)
paconn create \
  --api-def connector/apiDefinition.swagger.json \
  --api-prop connector/apiProperties.json \
  --script connector/script.csx
```

`paconn create` prints the new connector ID and writes a `settings.json` with the environment and connector
IDs (no secrets). To push later changes:

```bash
paconn update \
  --env <environment-guid> --cid <connector-id> \
  --api-def connector/apiDefinition.swagger.json \
  --api-prop connector/apiProperties.json \
  --script connector/script.csx
```

> **Known paconn limitation:** `paconn update` can fail while `stackOwner` is present in
> `apiProperties.json`. If that happens, run the update with a copy of the file that omits the `stackOwner`
> line. The property is only needed for certification.

### Option B: import in the maker portal

1. Open <https://make.powerapps.com> (or <https://make.powerautomate.com>) and pick your environment.
2. **Custom connectors** > **New custom connector** > **Import an OpenAPI file**. Name it
   `Collecta PayPal Sandbox` and select `connector/apiDefinition.swagger.json`.
3. **General**: host `api-m.sandbox.paypal.com`, base URL `/`, scheme HTTPS. Optionally set the icon
   background color to `#003087`.
4. **Security**: authentication type **Basic authentication**. Set the parameter labels to **Client ID**
   (username) and **Secret** (password).
5. **Definition**: nothing to change; the 14 actions are imported.
6. **Code**: turn on **Code Enabled**, upload `connector/script.csx` and leave the operation list empty (the
   code then applies to all operations) or select all 14.
7. **Create connector**.

The portal import does not read `apiProperties.json`; steps 3, 4 and 6 set the same values by hand.

## Create the connection

1. In the connector, open **Test** > **New connection** (or add any action of the connector to a flow or agent).
2. **Client ID**: the Client ID of your sandbox REST app.
3. **Secret**: the Secret of the same app.
4. Create the connection, then test **Generate next invoice number**: a `200` with an `invoice_number` confirms
   that the credentials and the custom code work.

The connection is not verified when it is created. Wrong credentials show up on the first call as
`401 invalid_client`.

## Operations

### Invoicing v2

| Operation ID | Request | What it does |
| --- | --- | --- |
| `GenerateNextInvoiceNumber` | `POST /v2/invoicing/generate-next-invoice-number` | Returns the next sequential invoice number of the account. |
| `CreateDraftInvoice` | `POST /v2/invoicing/invoices` | Creates a draft invoice. Returns `id`, `status`, `detail.metadata.recipient_view_url` (the customer's payment link) and `links`. |
| `SendInvoice` | `POST /v2/invoicing/invoices/{invoice_id}/send` | Sends a draft invoice to the customer; status becomes `SENT`. |
| `GetInvoice` | `GET /v2/invoicing/invoices/{invoice_id}` | Gets one invoice with status, amount due and amount paid. |
| `ListInvoices` | `GET /v2/invoicing/invoices` | Lists invoices by page (`page`, `page_size`, `total_required`). |
| `SearchInvoices` | `POST /v2/invoicing/search-invoices` | Finds invoices by status, recipient email, invoice number, date ranges and total amount range. |
| `RemindInvoice` | `POST /v2/invoicing/invoices/{invoice_id}/remind` | Emails a payment reminder for a sent, unpaid invoice. Returns `204`. |
| `CancelInvoice` | `POST /v2/invoicing/invoices/{invoice_id}/cancel` | Cancels a sent invoice. Returns `204`. |

### Orders v2

| Operation ID | Request | What it does |
| --- | --- | --- |
| `CreateOrder` | `POST /v2/checkout/orders` | Creates a checkout order for a one-time payment. The `approve` / `payer-action` link is the payment page for the customer. |
| `GetOrder` | `GET /v2/checkout/orders/{id}` | Gets an order with its status, payer and captures. |
| `CaptureOrder` | `POST /v2/checkout/orders/{id}/capture` | Captures the payment of an order the customer approved. |

### Payouts

| Operation ID | Request | What it does |
| --- | --- | --- |
| `CreatePayout` | `POST /v1/payments/payouts` | Sends money to one or more recipients (email, phone or PayPal ID). |
| `GetPayoutBatch` | `GET /v1/payments/payouts/{payout_batch_id}` | Gets the status of a payout batch and its items. |

### Reporting

| Operation ID | Request | What it does |
| --- | --- | --- |
| `ListTransactions` | `GET /v1/reporting/transactions` | Lists account transactions between `start_date` and `end_date` (always sent with `fields=all`). |

### Things worth knowing

- **Create draft invoice** exposes a useful subset of the PayPal invoice: `detail` (currency, number, date,
  payment term, note, memo), `invoicer`, `primary_recipients[].billing_info` and `items[]`. Required fields
  follow PayPal: `detail.currency_code`, and `items` with `name`, `quantity` and `unit_amount` on each item.
  A recipient is optional for a draft, but PayPal needs a recipient email to email the invoice.
- **Send, Remind and Cancel**: PayPal ignores a caller-supplied `subject` and `note` and uses its own standard
  email text. Set `send_to_recipient` to `false` to send nothing and share `recipient_view_url` yourself.
- **Remind**: PayPal allows only 2 reminders in a day.
- **Search invoices**: a date range needs both `start` and `end`, and an amount range needs both bounds.
  `payment_date_range` and `creation_date_range` take a date and time; the other ranges take a date.
- **Create order**: the customer has 6 hours to open the payment link. Pass
  `payment_source.paypal.experience_context.return_url` when you send the link yourself, otherwise PayPal shows
  an error page after approval. `invoice_id` must be unique per merchant account by default.
- **Create payout**: the connector requires `sender_batch_id` although PayPal does not, because that ID is what
  stops a retried request from paying twice.
- `invoicer.email_address` must be an email registered on the PayPal business account. Leave `invoicer` empty
  to use the account's own profile.
- **Remind** and **Cancel** only work on invoices that were sent. They return `204` with no body.
- **Capture order** returns `422 ORDER_NOT_APPROVED` until the customer has approved the order on the
  payment page.
- **List transactions** accepts a date range of at most 31 days, and new transactions can take a few hours
  to appear in the report.
- Amounts are strings (`"150.00"`), as in the PayPal API. Payouts use `currency`; everything else uses
  `currency_code`.
- Less common fields are marked `x-ms-visibility: advanced` and sit under **Show advanced options** in
  Power Automate.
- Each connector call makes two PayPal calls (token, then the operation). No token is cached between calls.

## Built with APIMatic Context Plugin

The connector definition was reviewed against the **APIMatic PayPal Context Plugin** for Claude Code
(`paypal@context-plugins`, version 0.4.1), so that PayPal facts come from a lookup and not from what a model
remembers.

**How it was used.** The plugin's skills point the coding agent at an *SDK map* that ships inside the
APIMatic-generated PayPal Server SDK source (API spec version 2.29). The map has one page per API group that
lists every operation and names the file declaring each request and response model. For each connector
operation the agent:

1. opened the operation's map page (`map/operations/Orders.md`, `map/operations/TransactionSearch.md`) for the
   inputs, query parameter wire names, return type and error statuses;
2. opened the declaring files that page names for the JSON wire names, required flags, length limits, enum
   values and the endpoint's behavioural notes;
3. ran a script that compares every property of the connector's `OrderRequest`, `Order` and `TransactionList`
   schemas with those files: 136 properties, with names, types, required flags, request enums and length
   limits all matching.

**Coverage.** The plugin covers Orders, Payments, Subscriptions, Transaction Search and Vault. That grounded 4
of the 14 operations: `CreateOrder`, `GetOrder`, `CaptureOrder` and `ListTransactions`. It has no Invoicing v2
or Payouts content, so the other 10 operations were checked against PayPal's published OpenAPI documents
(`invoicing_v2.json` and `payments_payouts_batch_v1.json`) instead.

**What the plugin corrected or clarified.**

- **The order's customer details had moved.** The `Order` response exposed only `payer`. The SDK source marks
  `payer` as deprecated and puts the customer's email, account ID and name under `payment_source.paypal`. The
  schema now exposes `payment_source.paypal` and labels `payer` as deprecated.
- **Reconciling a payment with a PayPal invoice.** The `SearchTransactions` request record shows that `fields`
  defaults to `transaction_info`, which confirmed that the connector must send `fields=all` to get payer and
  cart data. The `CartInformation` model revealed `cart_info.paypal_invoice_id`, the ID of the PayPal invoice
  a transaction paid. It was missing and is now in the response schema, next to `payer_name.full_name`
  (the previously exposed `alternate_full_name` is deprecated).
- **Order rules the agent has to know.** The model documentation supplied facts that are now in the tool
  descriptions: the customer has 6 hours to open the payment link, a missing `return_url` leads to an error
  page after approval, `invoice_id` must be unique per merchant account, and `description` is cut at 127
  characters. It also surfaced `payment_method_preference = IMMEDIATE_PAYMENT_REQUIRED` (no pending payments)
  and `status_details.reason` on a capture, both added.

The plugin found no wrong path, name, type or enum in the four operations it covers. The review against
PayPal's own OpenAPI documents did find errors in Invoicing: `payment_date_range` is a date-time range, date
and amount ranges need both bounds, the invoice status list had 4 missing values, and PayPal ignores a
supplied email `subject` and `note`.

Notes on the experience are in [docs/apimatic-notes.md](../docs/apimatic-notes.md).

## Smoke test

`tests/smoke-test.ps1` calls all 14 endpoints against the sandbox the same way the connector does. It runs on
Windows PowerShell 5.1 and PowerShell 7+.

```powershell
Copy-Item .env.example .env      # then fill in PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET
./tests/smoke-test.ps1 -RecipientEmail <sandbox-personal-email> -PayoutReceiver <sandbox-personal-email>
```

Credentials come from the environment variables `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`, or from a
git-ignored `.env` file in the repository root. The script prints one `PASS` / `FAIL` line per operation and
exits with a non-zero code if anything fails.

- It creates sandbox data: one invoice (created, sent, reminded, cancelled), one unapproved order and one
  payout of 1.00. Use `-SkipPayout` if the app does not have Payouts.
- **Capture order** is expected to answer `422 ORDER_NOT_APPROVED`, because nobody approved the test order.
  To test a real capture, approve an order with a sandbox Personal account and pass `-ApprovedOrderId <id>`.

## Going to production

This connector is deliberately bound to the sandbox. To target live PayPal, change `host` in
`apiDefinition.swagger.json` and `TokenUrl` in `script.csx` to `api-m.paypal.com`, and use live REST app
credentials in a new connection.

## License

MIT. See [LICENSE](../LICENSE) in the repository root.

PayPal is a trademark of PayPal, Inc. This project is not affiliated with or endorsed by PayPal.
