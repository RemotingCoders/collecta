# collecta
AI collections agent for Dynamics 365 &amp; Power Platform, powered by PayPal — custom connector, Dataverse solution and Copilot Studio agent.

## Repository layout

| Path | What it is |
| --- | --- |
| [connector/](connector/) | Power Platform custom connector for the PayPal REST API (sandbox): invoicing, orders, payouts and transaction reporting. Start with [connector/README.md](connector/README.md). |
| [tests/smoke-test.ps1](tests/smoke-test.ps1) | Calls every connector endpoint against the PayPal sandbox. |
| [.env.example](.env.example) | Template for the sandbox credentials used by the smoke test. Copy to `.env` (git-ignored). |
| [docs/apimatic-notes.md](docs/apimatic-notes.md) | Notes on using the APIMatic PayPal Context Plugin to ground the connector definition. See also [Built with APIMatic Context Plugin](connector/README.md#built-with-apimatic-context-plugin). |

## License

MIT. See [LICENSE](LICENSE).
