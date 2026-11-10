# Collecta demo seed

`seed.py` prepares a Dynamics 365 Sales environment and the PayPal sandbox for the Collecta demo. It is
idempotent: every step skips what already exists, so it can be run again at any time.

## What it creates

**Solution and schema** (in the environment, through the Dataverse Web API v9.2)

- Publisher **Remoting Coders** (prefix `rc`) and the unmanaged solution **Collecta**.
- Six columns on the **Invoice** table:

  | Column | Type | Purpose |
  | --- | --- | --- |
  | `rc_paypalinvoiceid` | Text (50) | PayPal invoice ID (`INV2-...`) |
  | `rc_paypalstatus` | Choice | Not sent, Draft, Sent, Unpaid, Partially paid, Paid, Cancelled |
  | `rc_paypalpaymenturl` | URL | Link the customer opens to pay in PayPal |
  | `rc_paypalsenton` | Date only | Date the invoice was sent through PayPal |
  | `rc_paypalamountpaid` | Currency | Amount paid according to PayPal (`payments.paid_amount`) |
  | `rc_paypalamountdue` | Currency | Amount still due according to PayPal (`due_amount`) |

- Table **PayPal Payment** (`rc_paypalpayment`, plural "PayPal Payments", user/team owned), one row per
  payment PayPal reports for an invoice:

  | Column | Type | Purpose |
  | --- | --- | --- |
  | `rc_name` | Text (primary) | For example `Fabrikam Inc - $500.00 - 2026-11-03` |
  | `rc_invoiceid` | Lookup to Invoice | Relationship `rc_invoice_rc_paypalpayment`, cascade delete |
  | `rc_transactionid` | Text (50), **alternate key** `rc_transactionid_key` | PayPal payment or transaction ID; used for upserts so a repeated webhook never creates a second row |
  | `rc_amount` | Currency | Payment amount |
  | `rc_paymentdate` | Date and time | When the payment was made |
  | `rc_method` | Choice | PayPal, Bank transfer, Cash, Check, Credit card, Debit card, Wire transfer, Other |
  | `rc_paymenttype` | Choice | PayPal, External |
  | `rc_note` | Multiline text | Note recorded with the payment |

- UI: a **PayPal** tab on the Invoice main form with the `rc_` columns and a **PayPal Payments** subgrid,
  and the **Active PayPal Payments** view with the payment columns. Everything is published.
- Solution hygiene: the Invoice table is added to the solution **without** subcomponents
  (`AddSolutionComponent` with `DoNotIncludeSubcomponents = true`), then only the `rc_` columns and the modified
  main form are added. `rc_paypalpayment` is added with all its subcomponents (columns, key, view).
- Security role **Collecta Reviewer** (root business unit, in the solution): Read at Organization level on
  Account, Contact, Invoice, Invoice Product, Price List, Currency and PayPal Payment, nothing else. Invoice
  Product has no privileges of its own and follows Invoice. The role is assigned to `COLLECTA_REVIEWER_USER`
  (default `judge@remotingcoders.com`) on top of the roles the user already has, such as Basic User.

**Reference data**

- Price list **Collecta USD** (currency USD). Invoice lines are write-in products, so no product catalog is needed.

**Demo data** (five accounts, one primary contact each, one invoice each)

| Account | Contact | Invoice | Amount | PayPal |
| --- | --- | --- | --- | --- |
| Acme Corp | Jane Cooper | Consulting - October 2026 (40 h x $150 + Premium support $500) | $6,500 | Not sent. This is the "Bill Acme for October" demo. |
| Contoso Retail | Robert Fox | Store fixtures - September 2026 | $2,400 | Sent 20 days ago, unpaid (overdue), no payments |
| Fabrikam Inc | Esther Howard | Network services - October 2026 | $1,250 | Sent 18 days ago, one external payment of $500 recorded: PARTIALLY_PAID, due $750, 1 PayPal Payment row, invoice stays Active |
| Northwind Traders | Cody Fisher | System integration - October 2026 | $3,100 | Sent 25 days ago, full external payment recorded: PAID, 1 PayPal Payment row, invoice closed as Paid / Complete |
| Tailspin Toys | Kristin Watson | Product photography - October 2026 | $780 | Not sent |

Every contact (and account) email is the sandbox buyer `PAYPAL_BUYER_EMAIL`, so judges can open the payment
links and pay with the sandbox Personal account.

The three "sent" invoices are really created and sent in the PayPal sandbox (Invoicing v2) with the invoice
date in the past, payment term NET_10 and `configuration.partial_payment.allow_partial_payment = true`, so
PayPal shows them as overdue and accepts partial payments. The PayPal ID, payment URL and sent-on date are
written to the `rc_` columns and the Dynamics invoice is set to **Billed**; everything else comes from the sync
rule below. `rc_paypalsenton` holds the back-dated invoice date, which is what the demo story needs; the real
send time is today.

## Sync rule: `sync_invoice_from_paypal(ctx, dataverse_invoice_id, paypal_invoice_id)`

PayPal is the source of truth. The function in `seed.py` is the reference for the Power Automate flow that
will do the same on PayPal webhooks:

1. `GET /v2/invoicing/invoices/{id}`.
2. Upsert one `rc_paypalpayment` per `payments.transactions[]`, keyed by the alternate key
   `rc_transactionid` = `payment_id` (`PATCH rc_paypalpayments(rc_transactionid='...')`). Method and type
   map from `transactions[].method` and `transactions[].type`.
3. Overwrite `rc_paypalstatus`, `rc_paypalamountpaid` (`payments.paid_amount`), `rc_paypalamountdue`
   (`due_amount`) and the native **Due Date** (`detail.payment_term.due_date`), so the standard overdue views
   work. Nothing is added up locally.
4. Keep the native Invoice state Active while the PayPal status is not paid. When it is `PAID`,
   `MARKED_AS_PAID` or `PAID_EXTERNAL`, close the invoice as **Paid / Complete** (statecode 2, statuscode
   100001) as the last step, because closed invoices are read-only. A closed invoice is reopened first if
   PayPal still reports it unpaid.

The seed runs it for every sent invoice at the end; `python seed.py --sync` runs only this step.

### Status mapping (PayPal `status` to `rc_paypalstatus`)

| PayPal status | `rc_paypalstatus` | Native invoice state |
| --- | --- | --- |
| `DRAFT` | Draft | Active |
| `SENT`, `SCHEDULED`, `SHARED`, `PAYMENT_PENDING` | Sent | Active (Billed) |
| `UNPAID` | Unpaid | Active (Billed) |
| `PARTIALLY_PAID`, `MARKED_AS_PARTIALLY_PAID` | Partially paid | Active (Billed) |
| `PAID`, `MARKED_AS_PAID`, `PAID_EXTERNAL` | Paid | Paid / Complete |
| `REFUNDED`, `PARTIALLY_REFUNDED`, `MARKED_AS_REFUNDED`, `REFUNDED_EXTERNAL` | Paid | unchanged (refunds are not modeled yet) |
| `CANCELLED`, `AUTO_CANCELLED` | Cancelled | unchanged |
| anything else | Sent | Active (Billed) |

Payment rows: `transactions[].method` maps to `rc_method` (`BANK_TRANSFER` to Bank transfer, `CREDIT_CARD` to
Credit card, and so on; unknown values to Other) and `transactions[].type` to `rc_paymenttype` (`PAYPAL`,
`EXTERNAL`).

## Prerequisites

- Python 3.9 or later.
- A user in the `https://collecta.crm.dynamics.com` environment with the **System Customizer** (or System
  Administrator) role: the script creates a publisher, a solution and columns.
- A PayPal sandbox REST app with **Invoicing** enabled, and a sandbox Personal account to act as the buyer.

## Configuration

Copy `.env.example` in the repository root to `.env` and fill in:

| Variable | Required | Default |
| --- | --- | --- |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | yes | - |
| `PAYPAL_BUYER_EMAIL` | no | `sb-h2ndk53805691@personal.example.com` |
| `DATAVERSE_URL` | no | `https://collecta.crm.dynamics.com` |
| `DATAVERSE_TENANT_ID` | no | `organizations` |
| `DATAVERSE_CLIENT_ID` | no | `51f81489-12ee-4a9e-aaae-a2591f45987d` (Microsoft's public client for Dataverse tooling) |
| `COLLECTA_REVIEWER_USER` | no | `judge@remotingcoders.com` |

Plain environment variables work too and take precedence over `.env`. Nothing is stored in the repository.

## Run

```bash
cd seed
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python seed.py
```

The first run opens a browser window to sign in to Dataverse (MSAL). Use `--device-code` to get a code to
enter at https://microsoft.com/devicelogin instead, for example on a machine without a browser. The token is
cached in `seed/.msal_token_cache.bin` (git-ignored), so later runs do not prompt.

The script prints one line per step (`created`, `exists`, `added`, `paypal`, `synced`) and ends with a summary
table: account, invoice, total, PayPal ID, PayPal status, paid, due and number of payments.

The alternate key is indexed asynchronously; the first run waits (up to 3 minutes) for it to become Active
before writing payments.

### Sync only

```bash
python seed.py --sync
```

Checks that the alternate key is Active, then runs `sync_invoice_from_paypal` for the demo invoices that are in
PayPal and prints the summary. Use it after paying an invoice in the sandbox to see the result in Dynamics.

### Re-seeding before a recording

```bash
python seed.py --reset        # asks for confirmation; add --yes to skip it
python seed.py
```

`--reset` deletes only the demo records, matched by the five account names: their PayPal Payment rows,
invoices (with lines), contacts and the accounts. Closed invoices are reopened first. Unpaid PayPal invoices
are cancelled without emailing anyone; the paid one is left as it is. The publisher, solution, tables, columns,
form, view and price list stay.

## Notes

- The PayPal invoice number comes from `generate-next-invoice-number`, so it is unique per run. The
  Dynamics invoice number is stored in the PayPal invoice as `reference`.
- Choice values for `rc_paypalstatus` are read back from the environment after the column is created, so an
  existing column with the same labels is used as it is.
- If the PayPal part fails after the Dynamics invoice was created, run the script again: an invoice that
  should be in PayPal but has no `rc_paypalinvoiceid` is sent on the next run.
- The script talks to the PayPal **sandbox** only; the host is fixed in `paypal_sandbox.py`.
