"""PayPal Invoicing v2 client (sandbox) used by the Collecta seed script.

Same calls the connector makes: client-credentials token, then bearer calls with
"Prefer: return=representation" on create.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import requests

SANDBOX = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.name = self.body.get("name") or self.body.get("error") or ""
        details = self.body.get("details") or []
        self.issue = details[0].get("issue", "") if details else ""
        message = self.body.get("message") or self.body.get("error_description") or str(body)[:300]
        parts = [p for p in (self.name, self.issue, message) if p]
        debug_id = self.body.get("debug_id")
        super().__init__("PayPal %s: %s%s" % (status, " - ".join(parts), " (debug_id %s)" % debug_id if debug_id else ""))


class PayPalSandbox:
    def __init__(self, client_id: str, client_secret: str, session: Optional[requests.Session] = None, base_url: str = SANDBOX):
        self.base = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = session or requests.Session()
        self._token: Optional[str] = None

    def token(self) -> str:
        if self._token:
            return self._token
        resp = self.http.post(self.base + "/v1/oauth2/token", data={"grant_type": "client_credentials"},
                              auth=(self.client_id, self.client_secret), headers={"Accept": "application/json"}, timeout=60)
        body = _json(resp)
        if resp.status_code != 200 or "access_token" not in body:
            raise PayPalError(resp.status_code, body)
        self._token = body["access_token"]
        return self._token

    def call(self, method: str, path: str, body: Any = None, representation: bool = False, ok: tuple = (200, 201, 202, 204)) -> dict:
        headers = {"Authorization": "Bearer " + self.token(), "Accept": "application/json", "Content-Type": "application/json"}
        if representation:
            headers["Prefer"] = "return=representation"
        data = json.dumps(body if body is not None else {}) if method == "POST" else None
        resp = self.http.request(method, self.base + path, data=data, headers=headers, timeout=60)
        parsed = _json(resp)
        if resp.status_code not in ok:
            raise PayPalError(resp.status_code, parsed)
        return parsed

    # -- Invoicing v2 ------------------------------------------------------------------------------
    def next_invoice_number(self) -> str:
        return self.call("POST", "/v2/invoicing/generate-next-invoice-number")["invoice_number"]

    def create_invoice(self, invoice: dict) -> dict:
        return self.call("POST", "/v2/invoicing/invoices", invoice, representation=True)

    def send_invoice(self, invoice_id: str, send_to_invoicer: bool = False) -> dict:
        return self.call("POST", "/v2/invoicing/invoices/%s/send" % invoice_id, {"send_to_invoicer": send_to_invoicer})

    def get_invoice(self, invoice_id: str) -> dict:
        return self.call("GET", "/v2/invoicing/invoices/" + invoice_id)

    def record_payment(self, invoice_id: str, amount: str, currency: str, payment_date: str, method: str = "BANK_TRANSFER", note: str = "") -> dict:
        body = {"method": method, "payment_date": payment_date, "amount": {"currency_code": currency, "value": amount}}
        if note:
            body["note"] = note
        return self.call("POST", "/v2/invoicing/invoices/%s/payments" % invoice_id, body)

    def cancel_invoice(self, invoice_id: str) -> None:
        self.call("POST", "/v2/invoicing/invoices/%s/cancel" % invoice_id, {"send_to_invoicer": False, "send_to_recipient": False})


def _json(resp: requests.Response) -> Any:
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"message": resp.text[:300]}
