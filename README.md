# collecta
AI collections agent for Dynamics 365 &amp; Power Platform, powered by PayPal — custom connector, Dataverse solution and Copilot Studio agent.

## Repository layout

| Path | What it is |
| --- | --- |
| [connector/](connector/) | Power Platform custom connector for the PayPal REST API (sandbox): invoicing, orders, payouts and transaction reporting. Start with [connector/README.md](connector/README.md). |
| [tests/smoke-test.ps1](tests/smoke-test.ps1) | Calls every connector endpoint against the PayPal sandbox. |
| [seed/](seed/) | Python script that prepares the demo: Collecta solution, `rc_` columns on Invoice, the PayPal Payment child table, form and views, Copilot-ready column descriptions and Dataverse search, price list, five demo accounts with invoices (three sent in the PayPal sandbox, one paid, one partially paid) and the PayPal-to-Dataverse sync rule. See [seed/README.md](seed/README.md). |
| [.env.example](.env.example) | Template for the sandbox credentials used by the smoke test. Copy to `.env` (git-ignored). |
| [docs/apimatic-notes.md](docs/apimatic-notes.md) | Notes on using the APIMatic PayPal Context Plugin to ground the connector definition. See also [Built with APIMatic Context Plugin](connector/README.md#built-with-apimatic-context-plugin). |

## Seed the demo environment

```bash
cp .env.example .env          # fill in PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET
cd seed && pip install -r requirements.txt
python seed.py                # sign in to Dataverse when the browser opens
python seed.py --reset        # remove the demo records before re-seeding
```

Details, variables and what gets created: [seed/README.md](seed/README.md).

## License

MIT. See [LICENSE](LICENSE).
