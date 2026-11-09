# APIMatic PayPal Context Plugin: usage notes

A short log of using the plugin to review `connector/apiDefinition.swagger.json`, kept for the hackathon
feedback question.

- **Date:** 2026-11-09
- **Plugin:** `paypal@context-plugins` 0.4.1 (APIMatic), installed in Claude Code at user scope
- **Source it grounds on:** `context-plugins/paypal-csharp-sdk`, branch `main` at commit `b4bafd5`
  (2026-10-13), API spec version 2.29, 5 API groups, 40 operations
- **Task:** verify a Power Platform custom connector (Swagger 2.0, raw REST, no SDK) for 14 PayPal operations

## Result in one paragraph

The plugin grounded 4 of the 14 operations (`CreateOrder`, `GetOrder`, `CaptureOrder`, `ListTransactions`).
For those it found nothing wrong in paths, names, types or enums, and it added real value through fields and
rules the first draft lacked. It could not help with the other 10 operations, because it has no Invoicing v2
or Payouts content, and Invoicing is the core of Collecta. Those were checked against PayPal's published
OpenAPI documents, which is where the actual errors were found.

## What worked well

- **Lookups are fast and deterministic.** Index (`sdk-map.md`) to operation page to the one file that declares
  a type. The four operations needed 2 map pages and a few dozen source files. No searching, no guessing.
- **One type per file, with wire names.** Each model file gives the JSON name (`[JsonPropertyName]`), the
  `required` flag, length limits and the enum values with a sentence per value. That made a scripted check
  possible: 136 connector properties compared with the SDK source, all matching.
- **Request records show the real defaults.** `Prefer` defaults to `return=minimal` and Transaction Search
  `fields` defaults to `transaction_info`. Both confirmed design decisions in the connector (the script adds
  `Prefer: return=representation`; the definition always sends `fields=all`).
- **Doc comments carry behaviour, not just shape.** Facts that went straight into the agent's tool
  descriptions: `Order.payer` is deprecated in favour of `payment_source.paypal`; the customer has 6 hours to
  open an order's payment link; `invoice_id` must be unique per merchant account; `description` is cut at 127
  characters; `IMMEDIATE_PAYMENT_REQUIRED` prevents pending captures; `cart_info.paypal_invoice_id` links a
  transaction to the PayPal invoice it paid.
- **It is honest about staleness.** The map states the spec version and the exact repo and branch it
  documents, and tells the agent to trust the compiler over the map if they disagree.

## Issues

1. **No Invoicing v2 and no Payouts.** The C#, Python and TypeScript SDK repos all have the same five map
   pages: Orders, Payments, Subscriptions, Transaction Search, Vault. 10 of our 14 operations were out of
   reach. This was the biggest limitation for an invoicing and collections project.
2. **Built for SDK consumers, not for REST or OpenAPI work.** The workflow assumes a C#, Python or TypeScript
   project, requires a `paypal-plan.md` at the repo root before any edit, and tells the agent to stop for any
   other language. A connector definition does not fit, so only the "pure question" lookup path was used and
   no plan file was written.
3. **The HTTP verb and route are not on the map pages.** They are in `src/Paypal/Api/<Group>.cs`. For a
   REST-level review that is the first fact needed, and it costs an extra file per API group. Header names
   (`PayPal-Request-Id`, `Prefer`) are only there too.
4. **Skills were not active in the session where the plugin was installed.** It was installed while the Claude
   Code session was already open, so the skills did not load and their `SKILL.md` files had to be read from
   the plugin cache by hand. After restarting VS Code all 27 skills loaded (as `paypal:dotnet-*`,
   `paypal:python-*` and `paypal:typescript-*`) with the same content, so the review results did not change.
   It was also installed at user scope, not in the repository, so teammates need to install it themselves.
5. **Some field docs are thin or dated.** `return_url` and `cancel_url` say only "Describes the URL." The
   useful return URL guidance sits on `Order.links` and names `application_context.return_url`, not the
   `payment_source.paypal.experience_context.return_url` field that the same SDK steers you to.
6. **Only a Sandbox environment is declared.** Fine for this sandbox connector, but the map gives no
   production base URL.
7. **Small glitches.** The map's auth section repeats "Oauth 2.0 authentication" five times, and the plugin
   README is titled "PayPal Server SDK SDK".

## Suggestions for APIMatic

- Add Invoicing v2 and Payouts.
- Put the verb, route and header names in each operation block of the map.
- Offer a language-neutral lookup mode for people writing OpenAPI files, connectors or raw HTTP calls.

## Draft answer for the feedback form

> We used the APIMatic PayPal Context Plugin to verify our Power Platform connector definition against the
> generated SDK source instead of model memory. Its map made lookups quick and exact, and the model files gave
> us wire names, required flags, limits and enum values that we checked by script (136 properties, all
> matching). It also taught us things we had missed: `payer` is deprecated in favour of
> `payment_source.paypal`, payment links expire after 6 hours, and `cart_info.paypal_invoice_id` ties a
> transaction to an invoice. The main gap is coverage: there is no Invoicing v2 or Payouts content, which is
> 10 of our 14 operations, and the workflow assumes you are writing SDK code in C#, Python or TypeScript
> rather than describing the REST API directly.
