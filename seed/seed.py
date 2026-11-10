#!/usr/bin/env python3
"""Seeds the Collecta demo into a Dynamics 365 Sales environment and the PayPal sandbox.

What it does (every step skips what already exists, so it can be re-run):
  1. Publisher "Remoting Coders" (prefix rc) and the unmanaged solution "Collecta". The Invoice table is added
     to the solution without subcomponents, then only the rc_ columns, the modified main form and the view.
     New table "PayPal Payment" (rc_paypalpayment, child of Invoice, cascade delete, alternate key on the
     PayPal transaction ID), a "PayPal" tab with the rc_ columns and a payments subgrid on the Invoice main
     form, an "Active PayPal Payments" view, and publish.
  2. Reference data: price list "Collecta USD".
  3. Five demo accounts with a primary contact and one invoice each. Three invoices are also created and sent in
     the PayPal sandbox (back-dated, NET_10, partial payments allowed). Northwind is paid in full, Fabrikam
     partially. sync_invoice_from_paypal() then mirrors PayPal into Dataverse for every sent invoice.
  4. --reset deletes the demo records (matched by account name) and cancels their unpaid PayPal invoices.

Everything sensitive comes from environment variables or the git-ignored .env at the repository root.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv

from dataverse import (COMPONENT_ATTRIBUTE, COMPONENT_ENTITY, COMPONENT_ROLE, COMPONENT_SYSTEM_FORM, Dataverse, DataverseError,
                       choice_column, date_only_column, datetime_column, entity_definition, memo_column, money_column,
                       msal_token_provider, one_to_many, primary_name_column, text_column, url_column)
from paypal_sandbox import PayPalError, PayPalSandbox

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

DEFAULT_DATAVERSE_URL = "https://collecta.crm.dynamics.com"
DEFAULT_CLIENT_ID = "51f81489-12ee-4a9e-aaae-a2591f45987d"  # Microsoft's public client ID for Dataverse tooling
DEFAULT_BUYER_EMAIL = "sb-h2ndk53805691@personal.example.com"
TOKEN_CACHE = HERE / ".msal_token_cache.bin"

PUBLISHER = {"uniquename": "RemotingCoders", "friendlyname": "Remoting Coders", "customizationprefix": "rc", "customizationoptionvalueprefix": 71000}
SOLUTION_NAME = "Collecta"
SOLUTION = {"uniquename": SOLUTION_NAME, "friendlyname": "Collecta", "version": "1.0.0.0",
            "description": "Collecta - AI collections agent powered by PayPal."}
PRICE_LIST_NAME = "Collecta USD"
CURRENCY = "USD"

PAYMENT_ENTITY = "rc_paypalpayment"
PAYMENT_SET = "rc_paypalpayments"
PAYMENT_RELATIONSHIP = "rc_invoice_rc_paypalpayment"
PAYMENT_KEY = "rc_transactionid_key"
VIEW_NAME = "Active PayPal Payments"
FORM_TAB = "tab_paypal"

STATUS_LABELS = ["Not sent", "Draft", "Sent", "Unpaid", "Partially paid", "Paid", "Cancelled"]
METHOD_LABELS = ["PayPal", "Bank transfer", "Cash", "Check", "Credit card", "Debit card", "Wire transfer", "Other"]
TYPE_LABELS = ["PayPal", "External"]
# PayPal invoice status -> rc_paypalstatus label. Documented in README.md (keep both in sync).
PAYPAL_TO_LABEL = {
    "DRAFT": "Draft",
    "SENT": "Sent", "SCHEDULED": "Sent", "SHARED": "Sent", "PAYMENT_PENDING": "Sent",
    "UNPAID": "Unpaid",
    "PARTIALLY_PAID": "Partially paid", "MARKED_AS_PARTIALLY_PAID": "Partially paid",
    "PAID": "Paid", "MARKED_AS_PAID": "Paid", "PAID_EXTERNAL": "Paid",
    "REFUNDED": "Paid", "PARTIALLY_REFUNDED": "Paid", "MARKED_AS_REFUNDED": "Paid", "REFUNDED_EXTERNAL": "Paid",
    "CANCELLED": "Cancelled", "AUTO_CANCELLED": "Cancelled",
}
# Statuses that close the Dynamics invoice as Paid / Complete.
PAYPAL_PAID = {"PAID", "MARKED_AS_PAID", "PAID_EXTERNAL"}

# Security role for the judges: read-only on the demo tables, assigned on top of Basic User.
REVIEWER_ROLE = "Collecta Reviewer"
REVIEWER_TABLES = ["account", "contact", "invoice", "invoicedetail", "pricelevel", "transactioncurrency", PAYMENT_ENTITY]
REVIEWER_USER = "judge@remotingcoders.com"
PAYPAL_METHOD_TO_LABEL = {"PAYPAL": "PayPal", "BANK_TRANSFER": "Bank transfer", "CASH": "Cash", "CHECK": "Check", "CREDIT_CARD": "Credit card",
                          "DEBIT_CARD": "Debit card", "WIRE_TRANSFER": "Wire transfer", "OTHER": "Other"}
PAYPAL_TYPE_TO_LABEL = {"PAYPAL": "PayPal", "EXTERNAL": "External"}

# Invoice statecode / statuscode (Dynamics 365 Sales)
INVOICE_ACTIVE_BILLED = (0, 4)
INVOICE_PAID_COMPLETE = (2, 100001)

# --------------------------------------------------------------------------------------------------
# Demo data (English, realistic). Line = (description, quantity, unit price, unit of measure).
# "payments": external payments recorded in PayPal; amount None = the full invoice total.
# --------------------------------------------------------------------------------------------------
DEMO = [
    {
        "account": {"name": "Acme Corp", "city": "Chicago", "state": "IL", "phone": "+1 312 555 0142", "website": "https://www.acme-corp.example.com",
                    "description": "Industrial equipment manufacturer. Monthly consulting retainer."},
        "contact": {"first": "Jane", "last": "Cooper", "title": "Accounts Payable Manager"},
        "invoice": {"name": "Consulting - October 2026", "lines": [
            ("Consulting services - October 2026", 40, 150.00, "HOURS"),
            ("Premium support - October", 1, 500.00, "QUANTITY"),
        ]},
        "paypal": None,  # the "Bill Acme for October" demo: the agent sends this one
    },
    {
        "account": {"name": "Contoso Retail", "city": "Seattle", "state": "WA", "phone": "+1 206 555 0187", "website": "https://www.contoso-retail.example.com",
                    "description": "Regional retail chain. Store fit-out projects."},
        "contact": {"first": "Robert", "last": "Fox", "title": "Store Operations Director"},
        "invoice": {"name": "Store fixtures - September 2026", "lines": [
            ("Custom display fixtures", 4, 450.00, "QUANTITY"),
            ("On-site installation", 6, 100.00, "HOURS"),
        ]},
        "paypal": {"sent_days_ago": 20, "payments": []},
    },
    {
        "account": {"name": "Fabrikam Inc", "city": "Austin", "state": "TX", "phone": "+1 512 555 0133", "website": "https://www.fabrikam.example.com",
                    "description": "Software company. Network and security services."},
        "contact": {"first": "Esther", "last": "Howard", "title": "IT Manager"},
        "invoice": {"name": "Network services - October 2026", "lines": [
            ("Network security assessment", 1, 850.00, "QUANTITY"),
            ("Firewall configuration", 4, 100.00, "HOURS"),
        ]},
        "paypal": {"sent_days_ago": 18, "payments": [{"amount": 500.00, "days_after": 5, "method": "BANK_TRANSFER", "note": "Partial payment by bank transfer"}]},
    },
    {
        "account": {"name": "Northwind Traders", "city": "Portland", "state": "OR", "phone": "+1 503 555 0168", "website": "https://www.northwind-traders.example.com",
                    "description": "Specialty food distributor. Inventory system project."},
        "contact": {"first": "Cody", "last": "Fisher", "title": "Finance Controller"},
        "invoice": {"name": "System integration - October 2026", "lines": [
            ("Inventory system integration", 1, 2500.00, "QUANTITY"),
            ("Staff training", 4, 150.00, "HOURS"),
        ]},
        "paypal": {"sent_days_ago": 25, "payments": [{"amount": None, "days_after": 7, "method": "BANK_TRANSFER", "note": "Bank transfer received"}]},
    },
    {
        "account": {"name": "Tailspin Toys", "city": "Denver", "state": "CO", "phone": "+1 303 555 0119", "website": "https://www.tailspin-toys.example.com",
                    "description": "Toy retailer. Marketing content services."},
        "contact": {"first": "Kristin", "last": "Watson", "title": "Marketing Manager"},
        "invoice": {"name": "Product photography - October 2026", "lines": [
            ("Product photography", 12, 65.00, "QUANTITY"),
        ]},
        "paypal": None,
    },
]


def money(value) -> str:
    return "%.2f" % float(value or 0)


def invoice_total(lines) -> float:
    return round(sum(q * p for _, q, p, _ in lines), 2)


def log(tag: str, message: str) -> None:
    print("[%-7s] %s" % (tag, message), flush=True)


def new_id() -> str:
    return "{%s}" % str(uuid.uuid4()).upper()


# --------------------------------------------------------------------------------------------------
# 1. Solution and schema
# --------------------------------------------------------------------------------------------------
def ensure_publisher(dv: Dataverse) -> dict:
    row = dv.first("publishers", "customizationprefix eq '%s'" % PUBLISHER["customizationprefix"],
                    "publisherid,uniquename,customizationprefix,customizationoptionvalueprefix")
    if row:
        log("exists", "Publisher '%s' (prefix %s)" % (row["uniquename"], row["customizationprefix"]))
        return row
    pid = dv.create("publishers", dict(PUBLISHER, description="Publisher of the Collecta solution."))
    log("created", "Publisher '%s' (prefix %s)" % (PUBLISHER["uniquename"], PUBLISHER["customizationprefix"]))
    return dict(PUBLISHER, publisherid=pid)


def ensure_solution(dv: Dataverse, publisher_id: str) -> str:
    sid = dv.solution_id(SOLUTION_NAME)
    if sid:
        log("exists", "Solution '%s'" % SOLUTION_NAME)
        return sid
    sid = dv.create("solutions", dict(SOLUTION, **{"publisherid@odata.bind": "/publishers(%s)" % publisher_id}))
    log("created", "Solution '%s'" % SOLUTION_NAME)
    return sid


def ensure_component(dv: Dataverse, solution_id: str, object_id: str, component_type: int, subcomponents: bool, what: str) -> None:
    existing = dv.solution_component(solution_id, object_id)
    if existing:
        if component_type == COMPONENT_ENTITY and not subcomponents and existing.get("rootcomponentbehavior") == 0:
            log("warn", "%s is in the solution WITH all subcomponents; remove and re-add it in the maker portal to keep the solution lean" % what)
        else:
            log("exists", "%s is in solution '%s'" % (what, SOLUTION_NAME))
        return
    dv.add_solution_component(SOLUTION_NAME, object_id, component_type, subcomponents)
    log("added", "%s to solution '%s'%s" % (what, SOLUTION_NAME, "" if subcomponents else " (without subcomponents)"))


def ensure_columns(dv: Dataverse, solution_id: str, entity: str, columns: list, track_components: bool = True) -> bool:
    """Creates the missing columns in the solution. With track_components, each column is also registered as its own
    solution component (needed for a table that is in the solution without subcomponents, such as Invoice)."""
    created = False
    for logical_name, payload in columns:
        if dv.attribute_exists(entity, logical_name):
            log("exists", "Column %s.%s" % (entity, logical_name))
        else:
            dv.create_attribute(entity, payload, SOLUTION_NAME)
            log("created", "Column %s.%s" % (entity, logical_name))
            created = True
        if track_components:
            ensure_component(dv, solution_id, dv.attribute_metadata_id(entity, logical_name), COMPONENT_ATTRIBUTE, False, "Column %s.%s" % (entity, logical_name))
    return created


def ensure_schema(dv: Dataverse, solution_id: str, option_prefix: int) -> None:
    base = option_prefix * 10000
    changed = False

    # -- Invoice: the table itself without subcomponents, then only our columns
    ensure_component(dv, solution_id, dv.entity_info("invoice")["MetadataId"], COMPONENT_ENTITY, False, "Table invoice")
    changed |= ensure_columns(dv, solution_id, "invoice", [
        ("rc_paypalinvoiceid", text_column("rc_paypalinvoiceid", "PayPal Invoice ID", "The ID of the invoice in PayPal (INV2-...).", 50)),
        ("rc_paypalstatus", choice_column("rc_paypalstatus", "PayPal Status", "The status of the invoice in PayPal.",
                                          [(base + i, text) for i, text in enumerate(STATUS_LABELS)])),
        ("rc_paypalpaymenturl", url_column("rc_paypalpaymenturl", "PayPal Payment URL", "The link the customer opens to view and pay the invoice in PayPal.")),
        ("rc_paypalsenton", date_only_column("rc_paypalsenton", "PayPal Sent On", "The date the invoice was sent through PayPal.")),
        ("rc_paypalamountpaid", money_column("rc_paypalamountpaid", "PayPal Amount Paid", "The amount paid so far according to PayPal.")),
        ("rc_paypalamountdue", money_column("rc_paypalamountdue", "PayPal Amount Due", "The amount still due according to PayPal.")),
    ])

    # -- PayPal Payment table, with all its subcomponents
    if dv.entity_exists(PAYMENT_ENTITY):
        log("exists", "Table %s" % PAYMENT_ENTITY)
    else:
        dv.create_entity(entity_definition(
            PAYMENT_ENTITY, "PayPal Payment", "PayPal Payments", "A payment recorded in PayPal against an invoice.",
            primary_name_column("rc_name", "Name", "Account, amount and date of the payment, for example Fabrikam Inc - $500.00 - 2026-11-03.")), SOLUTION_NAME)
        log("created", "Table %s (PayPal Payment)" % PAYMENT_ENTITY)
        changed = True
    payment_info = dv.entity_info(PAYMENT_ENTITY)
    ensure_component(dv, solution_id, payment_info["MetadataId"], COMPONENT_ENTITY, True, "Table %s" % PAYMENT_ENTITY)

    if dv.relationship_exists(PAYMENT_RELATIONSHIP):
        log("exists", "Relationship %s" % PAYMENT_RELATIONSHIP)
    else:
        dv.create_relationship(one_to_many(PAYMENT_RELATIONSHIP, "invoice", PAYMENT_ENTITY, "rc_invoiceid", "Invoice",
                                           "The invoice this payment belongs to.", "PayPal Payments", delete_cascade="Cascade"), SOLUTION_NAME)
        log("created", "Relationship %s (lookup rc_invoiceid, cascade delete)" % PAYMENT_RELATIONSHIP)
        changed = True

    # The payment table is in the solution with all subcomponents, so its columns and views travel with it.
    changed |= ensure_columns(dv, solution_id, PAYMENT_ENTITY, [
        ("rc_transactionid", text_column("rc_transactionid", "Transaction ID", "The PayPal payment or transaction ID. Unique per payment.", 50)),
        ("rc_amount", money_column("rc_amount", "Amount", "The amount of the payment.")),
        ("rc_paymentdate", datetime_column("rc_paymentdate", "Payment Date", "When the payment was made.")),
        ("rc_method", choice_column("rc_method", "Method", "How the payment was made.", [(base + i, text) for i, text in enumerate(METHOD_LABELS)])),
        ("rc_paymenttype", choice_column("rc_paymenttype", "Payment Type", "PayPal for a payment made through PayPal, External for a payment recorded by hand.",
                                         [(base + i, text) for i, text in enumerate(TYPE_LABELS)])),
        ("rc_note", memo_column("rc_note", "Note", "The note recorded with the payment.")),
    ], track_components=False)

    status = dv.key_status(PAYMENT_ENTITY, PAYMENT_KEY)
    if status is None:
        dv.create_key(PAYMENT_ENTITY, PAYMENT_KEY, "Transaction ID key", ["rc_transactionid"], SOLUTION_NAME)
        log("created", "Alternate key %s on rc_transactionid" % PAYMENT_KEY)
    else:
        log("exists", "Alternate key %s (%s)" % (PAYMENT_KEY, status))

    # -- UI: view and form
    view_id, view_changed = ensure_view(dv, payment_info["ObjectTypeCode"])  # a subcomponent of rc_paypalpayment, already in the solution
    form_id, form_changed = ensure_form(dv, view_id)
    ensure_component(dv, solution_id, form_id, COMPONENT_SYSTEM_FORM, False, "Invoice main form")
    changed |= view_changed or form_changed

    if changed:
        dv.publish("invoice", PAYMENT_ENTITY)
        log("done", "Published the Invoice and PayPal Payment tables")

    log("wait", "Waiting for the alternate key %s to become Active" % PAYMENT_KEY)
    dv.wait_for_key(PAYMENT_ENTITY, PAYMENT_KEY)


VIEW_COLUMNS = [("rc_name", 250), ("rc_paymentdate", 150), ("rc_amount", 120), ("rc_method", 130), ("rc_paymenttype", 110), ("rc_transactionid", 180), ("rc_invoiceid", 200)]


def ensure_view(dv: Dataverse, object_type_code: int) -> tuple:
    fetch = ('<fetch version="1.0" output-format="xml-platform" mapping="logical" distinct="false"><entity name="%s">'
             '%s<attribute name="rc_paypalpaymentid" /><order attribute="rc_paymentdate" descending="true" />'
             '<filter type="and"><condition attribute="statecode" operator="eq" value="0" /></filter></entity></fetch>'
             % (PAYMENT_ENTITY, "".join('<attribute name="%s" />' % c for c, _ in VIEW_COLUMNS)))
    layout = ('<grid name="resultset" object="%d" jump="rc_name" select="1" icon="1" preview="1"><row name="result" id="rc_paypalpaymentid">%s</row></grid>'
              % (object_type_code, "".join('<cell name="%s" width="%d" />' % (c, w) for c, w in VIEW_COLUMNS)))
    row = dv.first("savedqueries", "returnedtypecode eq '%s' and querytype eq 0 and name eq '%s'" % (PAYMENT_ENTITY, VIEW_NAME), "savedqueryid,layoutxml")
    if row:
        if 'name="rc_amount"' in (row.get("layoutxml") or ""):
            log("exists", "View '%s'" % VIEW_NAME)
            return row["savedqueryid"], False
        dv.update("savedqueries", row["savedqueryid"], {"fetchxml": fetch, "layoutxml": layout})
        log("updated", "View '%s' (columns)" % VIEW_NAME)
        return row["savedqueryid"], True
    vid = dv.create("savedqueries", {"name": VIEW_NAME, "returnedtypecode": PAYMENT_ENTITY, "querytype": 0, "isdefault": True,
                                     "description": "Active payments recorded in PayPal.", "fetchxml": fetch, "layoutxml": layout}, SOLUTION_NAME)
    log("created", "View '%s'" % VIEW_NAME)
    return vid, True


# Form control class IDs (classic form XML)
CLASS_TEXT = "{4273EDBD-AC1D-40d3-9FB2-095C621B552D}"
CLASS_PICKLIST = "{3EF39988-22BB-4f0b-BBBE-64B5A3748AEE}"
CLASS_DATE = "{5B773807-9FB2-42db-97C3-7A91EFF8ADFF}"
CLASS_MONEY = "{533B9E00-756B-4312-95A0-DC888637AC78}"
CLASS_SUBGRID = "{E7A81278-8635-4d9e-8D4D-59480B391C5B}"
FORM_FIELDS = [("rc_paypalstatus", "PayPal Status", CLASS_PICKLIST), ("rc_paypalinvoiceid", "PayPal Invoice ID", CLASS_TEXT),
               ("rc_paypalpaymenturl", "PayPal Payment URL", CLASS_TEXT), ("rc_paypalsenton", "PayPal Sent On", CLASS_DATE),
               ("rc_paypalamountpaid", "PayPal Amount Paid", CLASS_MONEY), ("rc_paypalamountdue", "PayPal Amount Due", CLASS_MONEY)]


def _labels(parent: ET.Element, text: str) -> None:
    ET.SubElement(ET.SubElement(parent, "labels"), "label", description=text, languagecode="1033")


def _section(column: ET.Element, name: str, title: str) -> ET.Element:
    sections = column.find("sections")
    if sections is None:
        sections = ET.SubElement(column, "sections")
    section = ET.SubElement(sections, "section", name=name, showlabel="true", showbar="false", locklevel="0", id=new_id(), IsUserDefined="0",
                            layout="varwidth", columns="1", labelwidth="115", celllabelalignment="Left", celllabelposition="Left")
    _labels(section, title)
    ET.SubElement(section, "rows")
    return section


def paypal_tab_xml(view_id: str) -> ET.Element:
    tab = ET.Element("tab", name=FORM_TAB, id=new_id(), IsUserDefined="1", locklevel="0", showlabel="true", expanded="true")
    _labels(tab, "PayPal")
    column = ET.SubElement(ET.SubElement(tab, "columns"), "column", width="100%")
    fields = _section(column, "section_paypal", "PayPal")
    rows = fields.find("rows")
    for name, title, class_id in FORM_FIELDS:
        cell = ET.SubElement(ET.SubElement(rows, "row"), "cell", id=new_id(), showlabel="true", locklevel="0")
        _labels(cell, title)
        ET.SubElement(cell, "control", id=name, classid=class_id, datafieldname=name, disabled="false")
    grid = _section(column, "section_paypal_payments", "PayPal Payments")
    cell = ET.SubElement(ET.SubElement(grid.find("rows"), "row"), "cell", id=new_id(), showlabel="true", rowspan="8", colspan="1", auto="false")
    _labels(cell, "PayPal Payments")
    control = ET.SubElement(cell, "control", id="PayPalPayments", classid=CLASS_SUBGRID, indicationOfSubgrid="true", uniqueid=new_id())
    params = ET.SubElement(control, "parameters")
    for key, value in [("ViewId", "{%s}" % view_id.upper()), ("IsUserView", "false"), ("RelationshipName", PAYMENT_RELATIONSHIP),
                       ("TargetEntityType", PAYMENT_ENTITY), ("AutoExpand", "Fixed"), ("EnableQuickFind", "false"), ("EnableViewPicker", "false"),
                       ("ViewIds", "{%s}" % view_id.upper()), ("EnableChartPicker", "false"), ("RecordsPerPage", "10")]:
        ET.SubElement(params, key).text = value
    return tab


def ensure_form(dv: Dataverse, view_id: str) -> tuple:
    """Adds a 'PayPal' tab (rc_ columns + payments subgrid) to the Invoice main form. Returns (formid, changed)."""
    forms = dv.query("systemforms", "objecttypecode eq 'invoice' and type eq 2", "formid,name,isdefault,formxml")
    if not forms:
        raise DataverseError(404, "No main form found for the Invoice table")
    form = next((f for f in forms if f.get("isdefault")), None) or next((f for f in forms if f.get("name") == "Invoice"), forms[0])
    root = ET.fromstring(form["formxml"])
    tabs = root.find("tabs")
    if tabs is None:
        tabs = ET.SubElement(root, "tabs")
    if any(t.get("name") == FORM_TAB for t in tabs.findall("tab")):
        log("exists", "Invoice main form '%s' already has the PayPal tab" % form["name"])
        return form["formid"], False
    tabs.append(paypal_tab_xml(view_id))
    dv.update("systemforms", form["formid"], {"formxml": ET.tostring(root, encoding="unicode")})
    log("updated", "Invoice main form '%s': added the PayPal tab (columns and payments subgrid)" % form["name"])
    return form["formid"], True


def ensure_reviewer_role(dv: Dataverse, solution_id: str, user_domainname: str) -> None:
    """Read-only role on the demo tables (Organization depth), in the Collecta solution, assigned to the judge."""
    root_bu = dv.first("businessunits", "_parentbusinessunitid_value eq null", "businessunitid,name")
    if not root_bu:
        raise DataverseError(404, "Root business unit not found")
    role = dv.first("roles", "name eq '%s' and _businessunitid_value eq %s" % (REVIEWER_ROLE, root_bu["businessunitid"]), "roleid")
    if role:
        role_id = role["roleid"]
        log("exists", "Security role '%s'" % REVIEWER_ROLE)
    else:
        role_id = dv.create("roles", {"name": REVIEWER_ROLE, "description": "Read-only access to the Collecta demo data (accounts, contacts, invoices, PayPal payments).",
                                      "businessunitid@odata.bind": "/businessunits(%s)" % root_bu["businessunitid"]}, SOLUTION_NAME)
        log("created", "Security role '%s' in business unit '%s'" % (REVIEWER_ROLE, root_bu.get("name")))
    ensure_component(dv, solution_id, role_id, COMPONENT_ROLE, False, "Security role '%s'" % REVIEWER_ROLE)

    role_privs = dv.role_privileges(role_id)
    current = {p["privilegeid"].lower() for p in role_privs}
    wanted, to_add = set(), []
    for table in REVIEWER_TABLES:
        reads = [p for p in dv.entity_privileges(table) if p.get("PrivilegeType") == "Read"]
        if not reads:
            log("info", "Table %s has no privileges of its own (it follows its parent table)" % table)
            continue
        for p in reads:
            wanted.add(p["PrivilegeId"].lower())
            if p["PrivilegeId"].lower() in current:
                continue
            depth = "Global" if p.get("CanBeGlobal") else "Deep" if p.get("CanBeDeep") else "Local" if p.get("CanBeLocal") else "Basic"
            to_add.append({"PrivilegeId": p["PrivilegeId"], "Depth": depth})
            log("add", "Privilege %s (%s) to '%s'" % (p.get("Name"), depth, REVIEWER_ROLE))
    if to_add:
        dv.add_role_privileges(role_id, to_add)
    else:
        log("exists", "Role '%s' already has read access to %s" % (REVIEWER_ROLE, ", ".join(REVIEWER_TABLES)))
    # New roles come with a few platform defaults (SharePoint, SDK message reads). Anything else that is not a read is worth a look.
    extra = sorted(p["name"] for p in role_privs if p["privilegeid"].lower() not in wanted and not p["name"].startswith("prvRead") and "SharePoint" not in p["name"])
    if extra:
        log("warn", "Role '%s' carries non-read privileges beyond the read-only set: %s" % (REVIEWER_ROLE, ", ".join(extra)))

    user = dv.first("systemusers", "domainname eq '%s'" % user_domainname, "systemuserid,fullname,_businessunitid_value")
    if not user:
        log("warn", "User %s not found in this environment; the role was not assigned" % user_domainname)
        return
    target_role_id = role_id
    if user["_businessunitid_value"] != root_bu["businessunitid"]:
        # Roles are per business unit; use the copy that lives in the user's unit.
        copy = dv.first("roles", "name eq '%s' and _businessunitid_value eq %s" % (REVIEWER_ROLE, user["_businessunitid_value"]), "roleid")
        if not copy:
            log("warn", "No copy of '%s' in the business unit of %s; assign it by hand" % (REVIEWER_ROLE, user_domainname))
            return
        target_role_id = copy["roleid"]
    if target_role_id in dv.user_role_ids(user["systemuserid"]):
        log("exists", "%s (%s) already has the role '%s'" % (user.get("fullname"), user_domainname, REVIEWER_ROLE))
    else:
        dv.assign_role(user["systemuserid"], target_role_id)
        log("assigned", "Role '%s' to %s (%s); existing roles such as Basic User are kept" % (REVIEWER_ROLE, user.get("fullname"), user_domainname))


def choice_values(dv: Dataverse, entity: str, attribute: str, expected: list) -> dict:
    """label -> option value, read back from the environment so existing columns are honored."""
    data = dv.get("EntityDefinitions(LogicalName='%s')/Attributes(LogicalName='%s')"
                  "/Microsoft.Dynamics.CRM.PicklistAttributeMetadata?$select=LogicalName&$expand=OptionSet" % (entity, attribute))
    values = {}
    for opt in data["OptionSet"]["Options"]:
        labels = opt.get("Label", {}).get("LocalizedLabels", [])
        if labels:
            values[labels[0]["Label"]] = opt["Value"]
    missing = [s for s in expected if s not in values]
    if missing:
        raise SystemExit("The %s.%s column is missing options %s. Add them or drop the column and re-run." % (entity, attribute, missing))
    return values


# --------------------------------------------------------------------------------------------------
# 2. Reference data
# --------------------------------------------------------------------------------------------------
def ensure_currency(dv: Dataverse) -> str:
    row = dv.first("transactioncurrencies", "isocurrencycode eq '%s'" % CURRENCY, "transactioncurrencyid")
    if row:
        log("exists", "Currency %s" % CURRENCY)
        return row["transactioncurrencyid"]
    cid = dv.create("transactioncurrencies", {"isocurrencycode": CURRENCY, "currencyname": "US Dollar", "currencysymbol": "$",
                                              "currencyprecision": 2, "exchangerate": 1.0})
    log("created", "Currency %s" % CURRENCY)
    return cid


def ensure_price_list(dv: Dataverse, currency_id: str) -> str:
    row = dv.first("pricelevels", "name eq '%s'" % PRICE_LIST_NAME, "pricelevelid")
    if row:
        log("exists", "Price list '%s'" % PRICE_LIST_NAME)
        return row["pricelevelid"]
    pid = dv.create("pricelevels", {"name": PRICE_LIST_NAME, "description": "Price list used by the Collecta demo invoices (write-in products).",
                                    "transactioncurrencyid@odata.bind": "/transactioncurrencies(%s)" % currency_id})
    log("created", "Price list '%s'" % PRICE_LIST_NAME)
    return pid


# --------------------------------------------------------------------------------------------------
# 3. Demo data
# --------------------------------------------------------------------------------------------------
class Context:
    def __init__(self, dv: Dataverse, pp: PayPalSandbox, currency_id: str, price_list_id: str, buyer_email: str,
                 status: dict, methods: dict, types: dict):
        self.dv, self.pp = dv, pp
        self.currency_id, self.price_list_id, self.buyer_email = currency_id, price_list_id, buyer_email
        self.status, self.methods, self.types = status, methods, types
        self.today = dt.date.today()


def ensure_account(ctx: Context, spec: dict) -> str:
    a = spec["account"]
    row = ctx.dv.first("accounts", "name eq '%s'" % a["name"], "accountid")
    if row:
        log("exists", "Account '%s'" % a["name"])
        return row["accountid"]
    aid = ctx.dv.create("accounts", {
        "name": a["name"], "telephone1": a["phone"], "websiteurl": a["website"], "emailaddress1": ctx.buyer_email,
        "address1_city": a["city"], "address1_stateorprovince": a["state"], "address1_country": "United States",
        "description": a["description"],
    })
    log("created", "Account '%s'" % a["name"])
    return aid


def ensure_contact(ctx: Context, spec: dict, account_id: str) -> str:
    c = spec["contact"]
    row = ctx.dv.first("contacts", "firstname eq '%s' and lastname eq '%s' and _parentcustomerid_value eq %s" % (c["first"], c["last"], account_id), "contactid")
    if row:
        log("exists", "Contact %s %s" % (c["first"], c["last"]))
        return row["contactid"]
    cid = ctx.dv.create("contacts", {
        "firstname": c["first"], "lastname": c["last"], "jobtitle": c["title"], "emailaddress1": ctx.buyer_email,
        "parentcustomerid_account@odata.bind": "/accounts(%s)" % account_id,
    })
    ctx.dv.update("accounts", account_id, {"primarycontactid@odata.bind": "/contacts(%s)" % cid})
    log("created", "Contact %s %s (primary contact)" % (c["first"], c["last"]))
    return cid


INVOICE_SELECT = "invoiceid,invoicenumber,name,totalamount,statecode,statuscode,rc_paypalinvoiceid,rc_paypalstatus,rc_paypalamountpaid,rc_paypalamountdue"


def ensure_invoice(ctx: Context, spec: dict, account_id: str) -> dict:
    inv = spec["invoice"]
    row = ctx.dv.first("invoices", "name eq '%s' and _customerid_value eq %s" % (inv["name"], account_id), INVOICE_SELECT)
    if row:
        log("exists", "Invoice '%s' (%s)" % (inv["name"], row.get("invoicenumber")))
        row["_created"] = False
        return row

    pp = spec["paypal"]
    invoice_date = ctx.today - dt.timedelta(days=pp["sent_days_ago"]) if pp else ctx.today
    due_date = invoice_date + dt.timedelta(days=10 if pp else 30)
    iid = ctx.dv.create("invoices", {
        "name": inv["name"],
        "customerid_account@odata.bind": "/accounts(%s)" % account_id,
        "pricelevelid@odata.bind": "/pricelevels(%s)" % ctx.price_list_id,
        "transactioncurrencyid@odata.bind": "/transactioncurrencies(%s)" % ctx.currency_id,
        "datedelivered": invoice_date.isoformat(),
        "duedate": due_date.isoformat(),
        "description": "Collecta demo invoice for %s." % spec["account"]["name"],
        "rc_paypalstatus": ctx.status["Not sent"],
    })
    for n, (desc, qty, price, _) in enumerate(inv["lines"], start=1):
        ctx.dv.create("invoicedetails", {
            "invoiceid@odata.bind": "/invoices(%s)" % iid,
            "isproductoverridden": True,
            "productdescription": desc,
            "quantity": qty,
            "priceperunit": price,
            "lineitemnumber": n,
        })
    row = ctx.dv.get("invoices(%s)?$select=%s" % (iid, INVOICE_SELECT))
    row["_created"] = True
    log("created", "Invoice '%s' (%s) with %d line(s), total %s %s" % (inv["name"], row.get("invoicenumber"), len(inv["lines"]), CURRENCY,
                                                                      money(row.get("totalamount") or invoice_total(inv["lines"]))))
    return row


def paypal_payload(ctx: Context, spec: dict, number: str, invoice_date: dt.date, d365_number: str) -> dict:
    a, c, inv = spec["account"], spec["contact"], spec["invoice"]
    return {
        "detail": {
            "currency_code": CURRENCY,
            "invoice_number": number,
            "invoice_date": invoice_date.isoformat(),
            "payment_term": {"term_type": "NET_10"},
            "reference": d365_number or "",
            "note": "Thank you for your business. Payment is due within 10 days.",
            "memo": "Collecta demo - %s" % a["name"],
        },
        "configuration": {
            "partial_payment": {"allow_partial_payment": True},
            "allow_tip": False,
        },
        "primary_recipients": [{"billing_info": {
            "name": {"given_name": c["first"], "surname": c["last"]},
            "business_name": a["name"],
            "email_address": ctx.buyer_email,
        }}],
        "items": [{
            "name": desc,
            "quantity": str(qty),
            "unit_amount": {"currency_code": CURRENCY, "value": money(price)},
            "unit_of_measure": uom,
        } for desc, qty, price, uom in inv["lines"]],
    }


def send_to_paypal(ctx: Context, spec: dict, invoice_row: dict) -> str:
    """Creates and sends the PayPal invoice, records the demo payments, stores ID/URL/date on the D365 invoice."""
    pp, inv = spec["paypal"], spec["invoice"]
    invoice_date = ctx.today - dt.timedelta(days=pp["sent_days_ago"])
    total = invoice_total(inv["lines"])

    number = ctx.pp.next_invoice_number()
    try:
        created = ctx.pp.create_invoice(paypal_payload(ctx, spec, number, invoice_date, invoice_row.get("invoicenumber")))
    except PayPalError as exc:
        if exc.issue != "DUPLICATE_INVOICE_NUMBER":
            raise
        number = "%s-%s" % (number, dt.datetime.now().strftime("%H%M%S"))
        created = ctx.pp.create_invoice(paypal_payload(ctx, spec, number, invoice_date, invoice_row.get("invoicenumber")))
    paypal_id = created["id"]
    log("paypal", "Created PayPal invoice %s (%s) for %s %s" % (paypal_id, number, CURRENCY, money(total)))

    ctx.pp.send_invoice(paypal_id)
    log("paypal", "Sent PayPal invoice %s to %s" % (paypal_id, ctx.buyer_email))

    for payment in pp["payments"]:
        amount = money(payment["amount"] if payment["amount"] is not None else total)
        payment_date = invoice_date + dt.timedelta(days=payment["days_after"])
        ctx.pp.record_payment(paypal_id, amount, CURRENCY, payment_date.isoformat(), method=payment["method"], note=payment["note"])
        log("paypal", "Recorded an external %s payment of %s %s on %s" % (payment["method"].lower().replace("_", " "), CURRENCY, amount, payment_date.isoformat()))

    view_url = ((created.get("detail") or {}).get("metadata") or {}).get("recipient_view_url", "")
    ctx.dv.update("invoices", invoice_row["invoiceid"], {
        "rc_paypalinvoiceid": paypal_id,
        "rc_paypalpaymenturl": view_url,
        "rc_paypalsenton": invoice_date.isoformat(),
        "statecode": INVOICE_ACTIVE_BILLED[0], "statuscode": INVOICE_ACTIVE_BILLED[1],
    })
    log("updated", "Invoice %s linked to PayPal %s and marked Billed" % (invoice_row.get("invoicenumber"), paypal_id))
    return paypal_id


# --------------------------------------------------------------------------------------------------
# Sync rule: PayPal is the source of truth. The Power Automate flow mirrors this function.
# --------------------------------------------------------------------------------------------------
def sync_invoice_from_paypal(ctx: Context, dataverse_invoice_id: str, paypal_invoice_id: str) -> dict:
    """Mirrors one PayPal invoice into Dataverse.

    1. Reads the invoice from PayPal.
    2. Upserts one rc_paypalpayment per payments.transactions[] (alternate key rc_transactionid), so a repeated
       webhook never creates a second payment row.
    3. Overwrites rc_paypalstatus, rc_paypalamountpaid (payments.paid_amount), rc_paypalamountdue (due_amount) and
       the native duedate (detail.payment_term.due_date); nothing is added up locally.
    4. Keeps the invoice Active while PayPal is not PAID; closes it as Paid / Complete when it is, as the last step,
       because closed invoices are read-only.
    """
    pp_inv = ctx.pp.get_invoice(paypal_invoice_id)
    pp_status = pp_inv.get("status", "")
    due_date = ((pp_inv.get("detail") or {}).get("payment_term") or {}).get("due_date")
    payments = pp_inv.get("payments") or {}
    transactions = payments.get("transactions") or []
    paid = float((payments.get("paid_amount") or {}).get("value") or 0)
    due = float((pp_inv.get("due_amount") or {}).get("value") or 0)

    invoice = ctx.dv.get("invoices(%s)?$select=invoiceid,invoicenumber,statecode,statuscode&$expand=customerid_account($select=name)" % dataverse_invoice_id)
    account_name = (invoice.get("customerid_account") or {}).get("name") or ""

    for n, tx in enumerate(transactions, start=1):
        tx_id = tx.get("payment_id") or "%s-%d" % (paypal_invoice_id, n)
        amount = float((tx.get("amount") or {}).get("value") or 0)
        pay_date = tx.get("payment_date") or ctx.today.isoformat()
        ctx.dv.upsert_by_key(PAYMENT_SET, "rc_transactionid", tx_id, {
            "rc_name": "%s - $%s - %s" % (account_name, money(amount), pay_date[:10]),
            "rc_invoiceid@odata.bind": "/invoices(%s)" % dataverse_invoice_id,
            "transactioncurrencyid@odata.bind": "/transactioncurrencies(%s)" % ctx.currency_id,
            "rc_transactionid": tx_id,
            "rc_amount": amount,
            "rc_paymentdate": pay_date if "T" in pay_date else pay_date + "T00:00:00Z",
            "rc_method": ctx.methods[PAYPAL_METHOD_TO_LABEL.get(tx.get("method"), "Other")],
            "rc_paymenttype": ctx.types[PAYPAL_TYPE_TO_LABEL.get(tx.get("type"), "External")],
            "rc_note": tx.get("note") or "",
        })

    if invoice.get("statecode") != 0:
        # Closed invoices are read-only: reopen before writing the PayPal columns.
        ctx.dv.update("invoices", dataverse_invoice_id, {"statecode": INVOICE_ACTIVE_BILLED[0], "statuscode": INVOICE_ACTIVE_BILLED[1]})

    fields = {
        "rc_paypalstatus": ctx.status[PAYPAL_TO_LABEL.get(pp_status, "Sent")],
        "rc_paypalamountpaid": paid,
        "rc_paypalamountdue": due,
    }
    if due_date:
        fields["duedate"] = due_date[:10]  # PayPal's due date drives the native Due Date, so overdue views work
    ctx.dv.update("invoices", dataverse_invoice_id, fields)
    if pp_status in PAYPAL_PAID:
        ctx.dv.update("invoices", dataverse_invoice_id, {"statecode": INVOICE_PAID_COMPLETE[0], "statuscode": INVOICE_PAID_COMPLETE[1]})

    result = {"status": pp_status, "label": PAYPAL_TO_LABEL.get(pp_status, "Sent"), "paid": paid, "due": due, "payments": len(transactions)}
    log("synced", "Invoice %s from PayPal %s: %s, paid %s, due %s%s, %d payment(s)%s" % (
        invoice.get("invoicenumber"), paypal_invoice_id, pp_status, money(paid), money(due), " by %s" % due_date[:10] if due_date else "", len(transactions),
        "; invoice closed as Paid" if pp_status in PAYPAL_PAID else ""))
    return result


def seed_one(ctx: Context, spec: dict) -> dict:
    account_id = ensure_account(ctx, spec)
    ensure_contact(ctx, spec, account_id)
    row = ensure_invoice(ctx, spec, account_id)
    summary = {"account": spec["account"]["name"], "invoice": "%s (%s)" % (spec["invoice"]["name"], row.get("invoicenumber") or "-"),
               "total": money(row.get("totalamount") or invoice_total(spec["invoice"]["lines"])),
               "paypal_id": "-", "paypal_status": "Not sent", "paid": "-", "due": "-", "payments": "0"}
    if spec["paypal"]:
        paypal_id = row.get("rc_paypalinvoiceid")
        if paypal_id:
            log("exists", "Invoice %s is already in PayPal as %s" % (row.get("invoicenumber"), paypal_id))
        else:
            paypal_id = send_to_paypal(ctx, spec, row)
        synced = sync_invoice_from_paypal(ctx, row["invoiceid"], paypal_id)
        summary.update({"paypal_id": paypal_id, "paypal_status": synced["label"], "paid": money(synced["paid"]), "due": money(synced["due"]),
                        "payments": str(synced["payments"])})
    return summary


# --------------------------------------------------------------------------------------------------
# 4. Reset
# --------------------------------------------------------------------------------------------------
def reset(dv: Dataverse, pp: PayPalSandbox, status: dict) -> None:
    open_values = {status[s] for s in ("Draft", "Sent", "Unpaid", "Partially paid")}
    for spec in DEMO:
        name = spec["account"]["name"]
        account = dv.first("accounts", "name eq '%s'" % name, "accountid")
        if not account:
            log("skip", "Account '%s' not found" % name)
            continue
        aid = account["accountid"]
        for inv in dv.query("invoices", "_customerid_value eq %s" % aid, "invoiceid,invoicenumber,name,rc_paypalinvoiceid,rc_paypalstatus,statecode"):
            if inv.get("rc_paypalinvoiceid") and inv.get("rc_paypalstatus") in open_values:
                try:
                    pp.cancel_invoice(inv["rc_paypalinvoiceid"])
                    log("paypal", "Cancelled PayPal invoice %s" % inv["rc_paypalinvoiceid"])
                except PayPalError as exc:
                    log("warn", "Could not cancel PayPal invoice %s: %s" % (inv["rc_paypalinvoiceid"], exc))
            payments = dv.query(PAYMENT_SET, "_rc_invoiceid_value eq %s" % inv["invoiceid"], "rc_paypalpaymentid")
            for p in payments:
                dv.delete(PAYMENT_SET, p["rc_paypalpaymentid"])
            if payments:
                log("deleted", "%d PayPal payment row(s) of invoice %s" % (len(payments), inv.get("invoicenumber")))
            if inv.get("statecode") != 0:
                # Paid and cancelled invoices are read-only: reopen, then delete.
                dv.update("invoices", inv["invoiceid"], {"statecode": INVOICE_ACTIVE_BILLED[0], "statuscode": INVOICE_ACTIVE_BILLED[1]})
            dv.delete("invoices", inv["invoiceid"])
            log("deleted", "Invoice '%s' (%s)" % (inv.get("name"), inv.get("invoicenumber")))
        for c in dv.query("contacts", "_parentcustomerid_value eq %s" % aid, "contactid,fullname"):
            dv.delete("contacts", c["contactid"])
            log("deleted", "Contact %s" % c.get("fullname"))
        dv.delete("accounts", aid)
        log("deleted", "Account '%s'" % name)


# --------------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------------
def print_summary(rows: list) -> None:
    headers = ["Account", "Invoice", "Total", "PayPal ID", "PayPal status", "Paid", "Due", "Payments"]
    keys = ["account", "invoice", "total", "paypal_id", "paypal_status", "paid", "due", "payments"]
    widths = [max(len(h), *(len(str(r[k])) for r in rows)) for h, k in zip(headers, keys)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print("\n" + line + "\n" + "-" * len(line))
    for r in rows:
        print("  ".join(str(r[k]).ljust(w) for k, w in zip(keys, widths)))
    print("Amounts in %s.\n" % CURRENCY)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Seed (or reset) the Collecta demo data in Dynamics 365 Sales and the PayPal sandbox.")
    parser.add_argument("--reset", action="store_true", help="delete the demo records and cancel their unpaid PayPal invoices")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation before --reset")
    parser.add_argument("--sync", action="store_true", help="only re-run sync_invoice_from_paypal for the demo invoices that are in PayPal")
    parser.add_argument("--device-code", action="store_true", help="sign in to Dataverse with a device code instead of a browser")
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")
    load_dotenv()  # a .env in the current directory, if any

    dataverse_url = env("DATAVERSE_URL", DEFAULT_DATAVERSE_URL).rstrip("/")
    client_id = env("DATAVERSE_CLIENT_ID", DEFAULT_CLIENT_ID)
    tenant = env("DATAVERSE_TENANT_ID", "organizations")
    buyer_email = env("PAYPAL_BUYER_EMAIL", DEFAULT_BUYER_EMAIL)
    paypal_id, paypal_secret = env("PAYPAL_CLIENT_ID"), env("PAYPAL_CLIENT_SECRET")
    if not paypal_id or not paypal_secret:
        print("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are not set. Copy .env.example to .env and fill them in.", file=sys.stderr)
        return 2

    token = msal_token_provider(dataverse_url, client_id, tenant, TOKEN_CACHE, "devicecode" if args.device_code else "auto")
    dv = Dataverse(dataverse_url, token)
    pp = PayPalSandbox(paypal_id, paypal_secret)

    try:
        who = dv.get("WhoAmI")
        log("ok", "Connected to %s (user %s)" % (dataverse_url, who.get("UserId")))
        pp.token()
        log("ok", "Connected to the PayPal sandbox")

        if args.reset:
            if not args.yes:
                names = ", ".join(s["account"]["name"] for s in DEMO)
                answer = input("This deletes the accounts %s with their contacts, invoices and PayPal payment rows, and cancels their unpaid "
                               "PayPal invoices. Type yes to continue: " % names)
                if answer.strip().lower() != "yes":
                    print("Cancelled.")
                    return 1
            if not dv.attribute_exists("invoice", "rc_paypalstatus"):
                raise SystemExit("The rc_ columns do not exist yet; nothing to reset.")
            reset(dv, pp, choice_values(dv, "invoice", "rc_paypalstatus", STATUS_LABELS))
            log("done", "Reset complete")
            return 0

        if args.sync:
            print("\n== Sync from PayPal")
            if not dv.entity_exists(PAYMENT_ENTITY):
                raise SystemExit("The schema does not exist yet; run the seed without --sync first.")
            log("wait", "Checking that the alternate key %s is Active" % PAYMENT_KEY)
            dv.wait_for_key(PAYMENT_ENTITY, PAYMENT_KEY)
            log("ok", "Alternate key %s is Active" % PAYMENT_KEY)
            ctx = Context(dv, pp, ensure_currency(dv), "", buyer_email, choice_values(dv, "invoice", "rc_paypalstatus", STATUS_LABELS),
                          choice_values(dv, PAYMENT_ENTITY, "rc_method", METHOD_LABELS), choice_values(dv, PAYMENT_ENTITY, "rc_paymenttype", TYPE_LABELS))
            rows = []
            for spec in DEMO:
                account = dv.first("accounts", "name eq '%s'" % spec["account"]["name"], "accountid")
                inv = account and dv.first("invoices", "name eq '%s' and _customerid_value eq %s" % (spec["invoice"]["name"], account["accountid"]), INVOICE_SELECT)
                if not inv or not inv.get("rc_paypalinvoiceid"):
                    log("skip", "%s: invoice not in PayPal" % spec["account"]["name"])
                    continue
                synced = sync_invoice_from_paypal(ctx, inv["invoiceid"], inv["rc_paypalinvoiceid"])
                rows.append({"account": spec["account"]["name"], "invoice": "%s (%s)" % (spec["invoice"]["name"], inv.get("invoicenumber")), "total": money(inv.get("totalamount")),
                             "paypal_id": inv["rc_paypalinvoiceid"], "paypal_status": synced["label"], "paid": money(synced["paid"]), "due": money(synced["due"]),
                             "payments": str(synced["payments"])})
            if rows:
                print_summary(rows)
            return 0

        print("\n== 1. Solution and schema")
        publisher = ensure_publisher(dv)
        solution_id = ensure_solution(dv, publisher["publisherid"])
        ensure_schema(dv, solution_id, int(publisher.get("customizationoptionvalueprefix") or PUBLISHER["customizationoptionvalueprefix"]))
        log("ok", "Alternate key %s is Active" % PAYMENT_KEY)
        status = choice_values(dv, "invoice", "rc_paypalstatus", STATUS_LABELS)
        methods = choice_values(dv, PAYMENT_ENTITY, "rc_method", METHOD_LABELS)
        types = choice_values(dv, PAYMENT_ENTITY, "rc_paymenttype", TYPE_LABELS)
        ensure_reviewer_role(dv, solution_id, env("COLLECTA_REVIEWER_USER", REVIEWER_USER))

        print("\n== 2. Reference data")
        currency_id = ensure_currency(dv)
        price_list_id = ensure_price_list(dv, currency_id)

        print("\n== 3. Demo data (contact and account emails: %s)" % buyer_email)
        ctx = Context(dv, pp, currency_id, price_list_id, buyer_email, status, methods, types)
        rows = [seed_one(ctx, spec) for spec in DEMO]
        print_summary(rows)
        return 0
    except (DataverseError, PayPalError) as exc:
        print("\nERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
