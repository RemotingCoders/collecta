# Collecta

**AI collections agent for Dynamics 365 Sales, powered by PayPal.**

Collecta helps a small business get paid. It lives inside **Copilot in Dynamics 365 Sales** as a connected
agent built with **Copilot Studio**: a seller asks "who owes me money?", "bill Acme for October" or "remind
Fabrikam", and Collecta reads the invoices in Dynamics 365, creates and sends the PayPal invoice, chases the
late ones with PayPal reminders and keeps the CRM in sync with what PayPal reports. Dynamics 365 is the system
of record for customers and invoices; PayPal is the source of truth for payments.

Built for the PayPal hackathon by Remoting Coders. Everything runs against the **PayPal sandbox**.

## Architecture

```
 Seller in Sales Hub                          PayPal (sandbox)
 "Who is overdue? Bill Acme."                 Invoicing v2 REST API, webhooks
        |                                              ^      |
        v                                              |      | INVOICING.INVOICE.* events
 Copilot in Dynamics 365 Sales  (native agent)         |      v
        |  connected agent                             |   Power Automate: "Collecta - PayPal webhook sync"
        v                                              |      |  re-fetches the invoice, upserts payments,
 Collecta agent  (Copilot Studio)                      |      |  updates totals, closes paid invoices
   tools: Preview invoice, Bill invoice in PayPal,     |      v
          Get unpaid invoices, Send invoice reminder,  |   Dataverse  (Collecta solution)
          Get invoice                                  |     Invoice + rc_ PayPal columns, PayPal Payment table,
        |                                              |     PayPal Invoices view, Collecta Reviewer role
        v                                              |
 Agent flows (Power Automate) --- Collecta PayPal Sandbox custom connector
```

| Layer | What it does | Where |
| --- | --- | --- |
| **Custom connector** "Collecta PayPal Sandbox" | Power Platform connector for the PayPal REST API (Invoicing v2, Orders v2, Payouts, Transaction reporting). A C# policy script exchanges the Client ID / Secret for an OAuth 2.0 bearer token on every call, so makers only create a connection. | [connector/](connector/) |
| **Dataverse solution** "Collecta" | Six `rc_` columns on Invoice (PayPal ID, status, payment link, sent-on date, amount paid, amount due), the **PayPal Payment** child table, the PayPal tab on the Invoice form, the **PayPal Invoices** view, Quick Find columns, the **Collecta Reviewer** role, Copilot-ready descriptions on every column. The connector, the flows, the connection references and the agent ship in the same solution. | [solution/](solution/) (zips) and [solution/src/](solution/src/) (unpacked) |
| **PayPal webhook sync flow** "Collecta - PayPal webhook sync" | HTTP-triggered Power Automate flow registered as a PayPal webhook. It takes only the invoice ID from the payload, re-reads the invoice from PayPal, upserts one PayPal Payment row per transaction (keyed by the PayPal payment ID), overwrites status, amounts and due date on the Dynamics invoice and closes it as Paid when PayPal says so. The same rule is implemented in Python in the seed as the reference. | [solution/src/Workflows/](solution/src/Workflows/), [seed/README.md](seed/README.md#sync-rule-sync_invoice_from_paypalctx-dataverse_invoice_id-paypal_invoice_id) |
| **Collecta agent** (Copilot Studio) | Generative agent with instructions and five tools: **Preview invoice**, **Bill invoice in PayPal** and **Get unpaid invoices** are agent flows over Dataverse and the connector; **Send invoice reminder** and **Get invoice** call the connector directly. It always summarizes and asks for confirmation before sending anything. It is published with *Let other agents connect* and added as a connected agent of the native **Copilot in Dynamics 365 Sales**, so sellers use it from Sales Hub without leaving the app. | [solution/src/bots/](solution/src/bots/), [solution/src/botcomponents/](solution/src/botcomponents/) |
| **Demo seed** | Python script that builds the solution schema, the role, the demo accounts and invoices, sends three of them in the PayPal sandbox and runs the sync rule. | [seed/](seed/) |

## Repository layout

| Path | What it is |
| --- | --- |
| [connector/](connector/) | Custom connector definition (`apiDefinition.swagger.json`, `apiProperties.json`, `script.csx`). Start with [connector/README.md](connector/README.md). |
| [solution/](solution/) | `Collecta_unmanaged.zip` and `Collecta_managed.zip` exported with `pac solution export`, plus `src/` unpacked with `pac solution unpack` so tables, forms, views, role, flows, connector and agent are readable in the repo. |
| [seed/](seed/) | Demo seed: schema, descriptions, search, role, demo data, PayPal sandbox invoices and the sync rule. See [seed/README.md](seed/README.md). |
| [tests/smoke-test.ps1](tests/smoke-test.ps1) | Calls every connector endpoint against the PayPal sandbox. |
| [docs/apimatic-notes.md](docs/apimatic-notes.md) | How the APIMatic PayPal Context Plugin was used to ground the connector definition. |
| [.env.example](.env.example) | Template for the sandbox credentials used by the seed and the smoke test. Copy to `.env` (git-ignored). |

## Setup

You need a Dynamics 365 Sales environment (System Customizer or System Administrator), Copilot Studio, Power
Automate, and a PayPal sandbox REST app with **Invoicing** enabled plus a sandbox Personal account to act as
the buyer. Nothing in this repository contains credentials; every secret is entered in the Power Platform or
read from `.env`.

1. **Import the connector.** Follow [connector/README.md](connector/README.md#create-the-connector) (`paconn
   create` or the maker portal). The connector is also inside the solution; importing it first lets you create
   the connection before the solution import asks for it.
2. **Import the solution.** In Power Apps, *Solutions > Import*, pick
   [solution/Collecta_managed.zip](solution/Collecta_managed.zip) (or the unmanaged one to customize). The import
   asks for the connection references of the flows and the agent.
3. **Create the connections.** One for *Collecta PayPal Sandbox* (sandbox Client ID and Secret, see
   [Create the connection](connector/README.md#create-the-connection)) and one for *Microsoft Dataverse*. Map
   them to the connection references when prompted, then make sure the four flows are turned on.
4. **Run the seed.**

   ```bash
   cp .env.example .env          # fill in PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET
   cd seed && pip install -r requirements.txt
   python seed.py                # sign in to Dataverse when the browser opens
   python seed.py --reset        # remove the demo records before re-seeding
   ```

   It creates the price list, five demo accounts with invoices (three sent in the PayPal sandbox: one overdue,
   one partially paid, one paid), assigns the Collecta Reviewer role and runs the sync rule. Details in
   [seed/README.md](seed/README.md).
5. **Register the PayPal webhook.** Open the flow *Collecta - PayPal webhook sync*, copy the HTTP POST URL of its
   trigger and add it in the PayPal Developer Dashboard (*Apps & Credentials > your sandbox app > Webhooks*)
   subscribed to the **Invoicing** events (`INVOICING.INVOICE.PAID`, `INVOICING.INVOICE.UPDATED`,
   `INVOICING.INVOICE.CANCELLED`, `INVOICING.INVOICE.REFUNDED`, and the rest of the invoice family). The trigger
   URL carries a signature (`sig=`): keep it out of the repository.
6. **Publish Collecta.** In Copilot Studio open the *Collecta* agent, check that the five tools point at the
   imported flows and connector, and *Publish*.
7. **Enable "Let other agents connect".** *Collecta > Settings > Security* (or the publish channel settings):
   turn on *Let other agents connect* so the native Sales agent can call it.
8. **Connect it to Copilot in Dynamics 365 Sales.** In Copilot Studio open **Copilot in Dynamics 365 Sales**
   (the native agent of the environment), go to *Agents > Add*, pick *Collecta*, give it a description of when
   to use it (collections, billing with PayPal, reminders), and **Publish** the Sales agent.

Sellers can now ask Copilot in Sales Hub "who owes me money?" and the request is routed to Collecta.

## Using Collecta from Copilot in Dynamics 365 Sales

A user who talks to Collecta from Sales Hub needs:

- Security roles **Basic User**, **Salesperson** and **Sales Copilot User** in the environment.
- Access to the **Sales Enterprise** app (the app's security roles, or an explicit assignment), plus a role that
  can read the Collecta data: **Collecta Reviewer** from the solution gives organization-level Read on Account,
  Contact, Invoice, Invoice Product, Price List, Currency and PayPal Payment, which is enough for the demo.
- The **Collecta** agent shared with them in Copilot Studio with **End user** access. Without the share the Sales
  agent still routes the question to Collecta, but Collecta answers that the user has no access.
- Copilot enabled for the environment and for the Sales Hub app (Sales settings > Copilot).

## Security notes

- **PayPal is the source of truth.** The webhook flow never trusts the payload: it reads only the PayPal invoice
  ID from it and re-fetches the invoice with `GET /v2/invoicing/invoices/{id}` through the connector before
  writing anything. Payments are upserted by PayPal payment ID, so a replayed or duplicated webhook changes
  nothing. Amounts are copied from PayPal, never added up locally.
- **Connections in the demo belong to the maker.** The agent tools and the flows run with the connections of
  the account that built them (connection references mapped at import). That is fine for a hackathon demo
  with one sandbox merchant. In production the PayPal connection should be per user (each seller creates their
  own connection to the connector) or a dedicated service principal, and the agent should run its Dataverse
  tools with the user's own connection so Dataverse security roles apply.
- **No secrets in the repository.** PayPal credentials live in the connection and in a local `.env` that is
  git-ignored; the exported solution contains the definition of the connection parameters, not their values,
  and no flow trigger URL. The seed's token cache is git-ignored too.
- **Webhook endpoint.** The flow trigger is an HTTP endpoint protected by the signature in its URL. A next step
  is to call PayPal's `POST /v1/notifications/verify-webhook-signature` with the transmission headers at the
  start of the flow and reject events that do not verify, and to filter on the `event_type` values the flow
  understands.
- **Confirmation before money moves.** The agent instructions require a summary and an explicit "yes" before
  billing or sending reminders, refuse to bill an invoice that already has a PayPal invoice ID, and never invent
  amounts, dates or emails.

## Testing (for judges)

Credentials for the demo environment (`https://collecta.crm.dynamics.com`, a Dynamics 365 user with the roles
above, and the PayPal sandbox buyer used to pay invoices) are provided privately through the Devpost submission.
They are never published in this repository.

Suggested walkthrough:

1. Open Sales Hub, go to **Invoices** and look at the **PayPal Invoices** view: five demo invoices, one overdue
   (Contoso Retail), one partially paid (Fabrikam Inc), one paid (Northwind Traders), two not sent.
2. Open Copilot in the Sales Hub side pane and ask *"Who owes me money?"*, then *"Bill Acme Corp for October"*
   and confirm when Collecta shows the summary. The invoice gets a PayPal ID, a payment link and status Sent.
3. Pay the Acme invoice (or part of the Fabrikam one) in the sandbox with the buyer account from the payment
   link. The webhook flow updates the invoice in Dynamics 365 within a minute: PayPal status, amount paid,
   amount due and a new row in **PayPal Payments**.
4. Ask *"Remind Contoso about their overdue invoice"* and confirm. PayPal sends the reminder email.

## License

MIT. See [LICENSE](LICENSE).
