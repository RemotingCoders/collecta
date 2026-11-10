"""Minimal Dataverse Web API (v9.2) client used by the Collecta seed script.

Authentication is interactive (browser) with a device-code fallback, through MSAL and the public
client ID that Microsoft ships for Dataverse tooling. Tokens are cached on disk next to this file so
repeated runs do not prompt again.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import msal
import requests

LANGUAGE_CODE = 1033
GUID_RE = re.compile(r"\(([0-9a-fA-F-]{36})\)\s*$")


class DataverseError(Exception):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__("HTTP %s%s: %s" % (status, " " + code if code else "", message))
        self.status = status
        self.code = code
        self.message = message


# --------------------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------------------
def msal_token_provider(resource_url: str, client_id: str, tenant: str, cache_path: Path, mode: str = "auto") -> Callable[[], str]:
    """Returns a function that yields a valid bearer token for the Dataverse environment.

    mode: "auto" (browser, then device code if the browser flow fails) or "devicecode".
    """
    scopes = [resource_url.rstrip("/") + "/.default"]
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(client_id, authority="https://login.microsoftonline.com/" + tenant, token_cache=cache)

    def save_cache() -> None:
        if cache.has_state_changed:
            cache_path.write_text(cache.serialize(), encoding="utf-8")
            try:
                cache_path.chmod(0o600)
            except OSError:
                pass

    def device_code() -> dict:
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise RuntimeError("Could not start the device code flow: %s" % flow.get("error_description", flow))
        print(flow["message"], flush=True)
        return app.acquire_token_by_device_flow(flow)

    def get_token() -> str:
        result = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(scopes, account=accounts[0])
        if not result:
            if mode == "devicecode":
                result = device_code()
            else:
                try:
                    print("Opening a browser window to sign in to Dataverse...", flush=True)
                    result = app.acquire_token_interactive(scopes, timeout=300)
                except Exception as exc:  # no browser, port blocked, etc.
                    print("Browser sign-in did not work (%s). Falling back to the device code flow." % exc, flush=True)
                    result = device_code()
        save_cache()
        if not result or "access_token" not in result:
            raise RuntimeError("Dataverse sign-in failed: %s" % (result or {}).get("error_description", result))
        return result["access_token"]

    return get_token


# --------------------------------------------------------------------------------------------------
# Web API client
# --------------------------------------------------------------------------------------------------
class Dataverse:
    def __init__(self, url: str, token_provider: Callable[[], str], session: Optional[requests.Session] = None):
        self.base = url.rstrip("/") + "/api/data/v9.2/"
        self.token_provider = token_provider
        self.http = session or requests.Session()

    # -- plumbing ---------------------------------------------------------------------------------
    def request(self, method: str, path: str, body: Any = None, headers: Optional[dict] = None, ok: tuple = (200, 201, 204)) -> requests.Response:
        h = {
            "Authorization": "Bearer " + self.token_provider(),
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "Accept": "application/json",
        }
        if body is not None:
            h["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            h.update(headers)
        url = path if path.startswith("http") else self.base + path
        resp = self.http.request(method, url, data=json.dumps(body) if body is not None else None, headers=h, timeout=120)
        if resp.status_code not in ok:
            code, message = "", resp.text[:800]
            try:
                err = resp.json().get("error", {})
                code, message = err.get("code", ""), err.get("message", message)
            except ValueError:
                pass
            raise DataverseError(resp.status_code, message, code)
        return resp

    def get(self, path: str) -> dict:
        return self.request("GET", path).json()

    def create(self, entity_set: str, data: dict, solution: Optional[str] = None) -> str:
        """Creates a record and returns its GUID (taken from the OData-EntityId header)."""
        headers = {"If-None-Match": "null"}
        if solution:
            headers["MSCRM.SolutionUniqueName"] = solution
        resp = self.request("POST", entity_set, data, headers)
        m = GUID_RE.search(resp.headers.get("OData-EntityId", ""))
        if not m:
            raise DataverseError(resp.status_code, "Create returned no OData-EntityId header for " + entity_set)
        return m.group(1).lower()

    def update(self, entity_set: str, record_id: str, data: dict) -> None:
        self.request("PATCH", "%s(%s)" % (entity_set, record_id), data, {"If-Match": "*"})

    def upsert_by_key(self, entity_set: str, key_attribute: str, key_value: str, data: dict) -> None:
        """PATCH on an alternate key: creates the record when the key is new, updates it otherwise."""
        self.request("PATCH", "%s(%s='%s')" % (entity_set, key_attribute, key_value.replace("'", "''")), data)

    def delete(self, entity_set: str, record_id: str) -> None:
        self.request("DELETE", "%s(%s)" % (entity_set, record_id))

    def action(self, name: str, parameters: dict) -> dict:
        resp = self.request("POST", name, parameters)
        return resp.json() if resp.content else {}

    def query(self, entity_set: str, filter_: str, select: str, top: Optional[int] = None) -> list:
        path = "%s?$select=%s&$filter=%s" % (entity_set, select, filter_)
        if top:
            path += "&$top=%d" % top
        rows = []
        while path:
            data = self.get(path)
            rows.extend(data.get("value", []))
            path = data.get("@odata.nextLink")
        return rows

    def first(self, entity_set: str, filter_: str, select: str) -> Optional[dict]:
        rows = self.query(entity_set, filter_, select, top=1)
        return rows[0] if rows else None

    # -- metadata ----------------------------------------------------------------------------------
    def attribute_exists(self, entity: str, logical_name: str) -> bool:
        try:
            self.get("EntityDefinitions(LogicalName='%s')/Attributes(LogicalName='%s')?$select=LogicalName" % (entity, logical_name))
            return True
        except DataverseError as exc:
            if exc.status == 404:
                return False
            raise

    def create_attribute(self, entity: str, payload: dict, solution: str) -> None:
        self.request("POST", "EntityDefinitions(LogicalName='%s')/Attributes" % entity, payload,
                     {"MSCRM.SolutionUniqueName": solution, "If-None-Match": "null"})

    def entity_exists(self, logical_name: str) -> bool:
        try:
            self.get("EntityDefinitions(LogicalName='%s')?$select=LogicalName" % logical_name)
            return True
        except DataverseError as exc:
            if exc.status == 404:
                return False
            raise

    def entity_info(self, logical_name: str) -> dict:
        return self.get("EntityDefinitions(LogicalName='%s')?$select=MetadataId,ObjectTypeCode,PrimaryIdAttribute" % logical_name)

    def attribute_metadata_id(self, entity: str, logical_name: str) -> str:
        return self.get("EntityDefinitions(LogicalName='%s')/Attributes(LogicalName='%s')?$select=MetadataId" % (entity, logical_name))["MetadataId"]

    def create_entity(self, payload: dict, solution: str) -> None:
        self.request("POST", "EntityDefinitions", payload, {"MSCRM.SolutionUniqueName": solution, "If-None-Match": "null"})

    def relationship_exists(self, schema_name: str) -> bool:
        try:
            self.get("RelationshipDefinitions(SchemaName='%s')?$select=SchemaName" % schema_name)
            return True
        except DataverseError as exc:
            if exc.status == 404:
                return False
            raise

    def create_relationship(self, payload: dict, solution: str) -> None:
        self.request("POST", "RelationshipDefinitions", payload, {"MSCRM.SolutionUniqueName": solution, "If-None-Match": "null"})

    def key_status(self, entity: str, key_logical_name: str) -> Optional[str]:
        """None when the key does not exist, otherwise its EntityKeyIndexStatus (Pending, InProgress, Active, Failed)."""
        try:
            return self.get("EntityDefinitions(LogicalName='%s')/Keys(LogicalName='%s')?$select=EntityKeyIndexStatus" % (entity, key_logical_name))["EntityKeyIndexStatus"]
        except DataverseError as exc:
            if exc.status == 404:
                return None
            raise

    def create_key(self, entity: str, schema_name: str, display: str, attributes: list, solution: str) -> None:
        self.request("POST", "EntityDefinitions(LogicalName='%s')/Keys" % entity,
                     {"SchemaName": schema_name, "DisplayName": label(display), "KeyAttributes": attributes},
                     {"MSCRM.SolutionUniqueName": solution, "If-None-Match": "null"})

    def wait_for_key(self, entity: str, key_logical_name: str, timeout: int = 180) -> None:
        """Alternate keys are indexed asynchronously; upserts fail until the key is Active."""
        deadline = time.time() + timeout
        while True:
            status = self.key_status(entity, key_logical_name)
            if status == "Active":
                return
            if status == "Failed":
                raise DataverseError(500, "Alternate key %s on %s failed to activate" % (key_logical_name, entity))
            if time.time() > deadline:
                raise DataverseError(504, "Alternate key %s on %s is still %s after %ds" % (key_logical_name, entity, status, timeout))
            time.sleep(5)

    def publish(self, *entities: str) -> None:
        xml = "<importexportxml><entities>%s</entities></importexportxml>" % "".join("<entity>%s</entity>" % e for e in entities)
        self.request("POST", "PublishXml", {"ParameterXml": xml})

    # -- solution components -----------------------------------------------------------------------
    def solution_id(self, unique_name: str) -> Optional[str]:
        row = self.first("solutions", "uniquename eq '%s'" % unique_name, "solutionid")
        return row["solutionid"] if row else None

    def solution_component(self, solution_id: str, object_id: str) -> Optional[dict]:
        return self.first("solutioncomponents", "_solutionid_value eq %s and objectid eq %s" % (solution_id, object_id),
                          "solutioncomponentid,componenttype,rootcomponentbehavior")

    # -- security roles ----------------------------------------------------------------------------
    def entity_privileges(self, logical_name: str) -> list:
        """The privileges of a table: dicts with PrivilegeId, Name, PrivilegeType (Read, Create...) and CanBeGlobal/Deep/Local/Basic."""
        return self.get("EntityDefinitions(LogicalName='%s')?$select=Privileges" % logical_name).get("Privileges", [])

    def role_privileges(self, role_id: str) -> list:
        """The privileges of a role: dicts with privilegeid and name."""
        return self.get("roles(%s)?$select=roleid&$expand=roleprivileges_association($select=privilegeid,name)" % role_id).get("roleprivileges_association", [])

    def add_role_privileges(self, role_id: str, privileges: list) -> None:
        """privileges: list of {"PrivilegeId": guid, "Depth": "Basic|Local|Deep|Global"}."""
        self.request("POST", "roles(%s)/Microsoft.Dynamics.CRM.AddPrivilegesRole" % role_id, {"Privileges": privileges})

    def user_role_ids(self, user_id: str) -> set:
        return {r["roleid"] for r in self.get("systemusers(%s)/systemuserroles_association?$select=roleid" % user_id).get("value", [])}

    def assign_role(self, user_id: str, role_id: str) -> None:
        self.request("POST", "systemusers(%s)/systemuserroles_association/$ref" % user_id, {"@odata.id": self.base + "roles(%s)" % role_id})

    def add_solution_component(self, solution: str, object_id: str, component_type: int, subcomponents: bool) -> None:
        self.action("AddSolutionComponent", {
            "ComponentId": object_id,
            "ComponentType": component_type,
            "SolutionUniqueName": solution,
            "AddRequiredComponents": False,
            "DoNotIncludeSubcomponents": not subcomponents,
        })


# --------------------------------------------------------------------------------------------------
# Column definition builders (payloads for EntityDefinitions(...)/Attributes)
# --------------------------------------------------------------------------------------------------
def label(text: str) -> dict:
    loc = {"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": text, "LanguageCode": LANGUAGE_CODE, "IsManaged": False}
    return {"@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [loc], "UserLocalizedLabel": dict(loc)}


def _common(schema_name: str, display: str, description: str) -> dict:
    return {
        "SchemaName": schema_name,
        "DisplayName": label(display),
        "Description": label(description),
        "RequiredLevel": {"Value": "None", "CanBeChanged": True, "ManagedPropertyLogicalName": "canmodifyrequirementlevelsettings"},
    }


def text_column(schema_name: str, display: str, description: str, max_length: int, fmt: str = "Text") -> dict:
    d = _common(schema_name, display, description)
    d.update({
        "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
        "AttributeType": "String",
        "AttributeTypeName": {"Value": "StringType"},
        "FormatName": {"Value": fmt},
        "MaxLength": max_length,
    })
    return d


def url_column(schema_name: str, display: str, description: str, max_length: int = 500) -> dict:
    return text_column(schema_name, display, description, max_length, fmt="Url")


def date_only_column(schema_name: str, display: str, description: str) -> dict:
    d = _common(schema_name, display, description)
    d.update({
        "@odata.type": "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata",
        "AttributeType": "DateTime",
        "AttributeTypeName": {"Value": "DateTimeType"},
        "Format": "DateOnly",
        "DateTimeBehavior": {"Value": "DateOnly"},
        "ImeMode": "Disabled",
    })
    return d


def datetime_column(schema_name: str, display: str, description: str) -> dict:
    d = _common(schema_name, display, description)
    d.update({
        "@odata.type": "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata",
        "AttributeType": "DateTime",
        "AttributeTypeName": {"Value": "DateTimeType"},
        "Format": "DateAndTime",
        "DateTimeBehavior": {"Value": "UserLocal"},
        "ImeMode": "Disabled",
    })
    return d


def money_column(schema_name: str, display: str, description: str) -> dict:
    d = _common(schema_name, display, description)
    d.update({
        "@odata.type": "Microsoft.Dynamics.CRM.MoneyAttributeMetadata",
        "AttributeType": "Money",
        "AttributeTypeName": {"Value": "MoneyType"},
        "ImeMode": "Disabled",
        "MinValue": 0.0,
        "MaxValue": 922337203685477.0,
        "Precision": 2,
        "PrecisionSource": 2,  # use the currency's precision
        "IsBaseCurrency": False,
    })
    return d


def memo_column(schema_name: str, display: str, description: str, max_length: int = 2000) -> dict:
    d = _common(schema_name, display, description)
    d.update({
        "@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
        "AttributeType": "Memo",
        "AttributeTypeName": {"Value": "MemoType"},
        "Format": "TextArea",
        "ImeMode": "Disabled",
        "MaxLength": max_length,
        "IsLocalizable": False,
    })
    return d


def primary_name_column(schema_name: str, display: str, description: str, max_length: int = 200) -> dict:
    d = text_column(schema_name, display, description, max_length)
    d["IsPrimaryName"] = True
    return d


def entity_definition(schema_name: str, display: str, plural: str, description: str, primary_name: dict, ownership: str = "UserOwned") -> dict:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
        "SchemaName": schema_name,
        "DisplayName": label(display),
        "DisplayCollectionName": label(plural),
        "Description": label(description),
        "OwnershipType": ownership,
        "IsActivity": False,
        "HasActivities": False,
        "HasNotes": False,
        "PrimaryNameAttribute": primary_name["SchemaName"].lower(),
        "Attributes": [primary_name],
    }


def one_to_many(schema_name: str, referenced_entity: str, referencing_entity: str, lookup_schema_name: str,
                lookup_display: str, lookup_description: str, menu_label: str, delete_cascade: str = "Cascade") -> dict:
    lookup = _common(lookup_schema_name, lookup_display, lookup_description)
    lookup.update({
        "@odata.type": "Microsoft.Dynamics.CRM.LookupAttributeMetadata",
        "AttributeType": "Lookup",
        "AttributeTypeName": {"Value": "LookupType"},
        "Format": "None",
    })
    lookup["RequiredLevel"] = {"Value": "ApplicationRequired", "CanBeChanged": True, "ManagedPropertyLogicalName": "canmodifyrequirementlevelsettings"}
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.OneToManyRelationshipMetadata",
        "SchemaName": schema_name,
        "ReferencedEntity": referenced_entity,
        "ReferencingEntity": referencing_entity,
        "RelationshipType": "OneToManyRelationship",
        "IsHierarchical": False,
        "IsValidForAdvancedFind": True,
        "SecurityTypes": "Append",
        "AssociatedMenuConfiguration": {"Behavior": "UseLabel", "Group": "Details", "Label": label(menu_label), "Order": 10000},
        "CascadeConfiguration": {"Assign": "NoCascade", "Delete": delete_cascade, "Merge": "NoCascade", "Reparent": "NoCascade",
                                 "Share": "NoCascade", "Unshare": "NoCascade", "RollupView": "NoCascade"},
        "Lookup": lookup,
    }


# Solution component types (AddSolutionComponent)
COMPONENT_ENTITY = 1
COMPONENT_ATTRIBUTE = 2
COMPONENT_ENTITY_KEY = 14
COMPONENT_ROLE = 20
COMPONENT_SAVED_QUERY = 26
COMPONENT_SYSTEM_FORM = 60


def choice_column(schema_name: str, display: str, description: str, options: list) -> dict:
    """options: list of (value, label)."""
    d = _common(schema_name, display, description)
    d.update({
        "@odata.type": "Microsoft.Dynamics.CRM.PicklistAttributeMetadata",
        "AttributeType": "Picklist",
        "AttributeTypeName": {"Value": "PicklistType"},
        "SourceTypeMask": 0,
        "OptionSet": {
            "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata",
            "IsGlobal": False,
            "OptionSetType": "Picklist",
            "Options": [{"Value": value, "Label": label(text)} for value, text in options],
        },
    })
    return d


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)
