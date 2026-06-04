"""DHL Express MCP — tracking + label/return generation via MyDHL API.

Wraps the MyDHL API (DHL Express REST, https://developer.dhl.com/api-reference/mydhl-api-dhl-express).
Single-tenant: one DHL Express account, credentials in env. End users
authenticate to claude.ai with Google Workspace and reach this through the
gateway; they never see a DHL login.

UNLIKE every other MCP in this gateway, this one performs WRITES (creating
shipments / return shipments produces real labels and can trigger billing on
the DHL account). Two structural guards keep that safe:
  1. The gateway auth chain (x-worker-shared-secret + Workspace email) is the
     only path in — same guard as the read-only MCPs.
  2. DHL_BASE_URL defaults to the MyDHL **test** environment
     (.../mydhlapi/test). Production label creation only happens once someone
     deliberately sets DHL_BASE_URL to the production base in the EC2 .env.
     Keep it on test until the create-shipment payloads are validated.

Auth model:
  MyDHL API uses HTTP Basic auth: Authorization: Basic base64(API_KEY:API_SECRET).
  The API key/secret pair comes from the DHL developer portal app bound to the
  DHL Express account. No token refresh — the Basic header is static.

Tool surface:
  read:
    - track_shipment(tracking_number)        : GET /shipments/{n}/tracking
    - validate_address(country_code, postal_code, ...) : GET /address-validate
    - get_rates(payload)                     : POST /rates (quote only, no booking)
  write (two-step: kill-switch DHL_ALLOW_CREATE + confirm=true; pharmacy party
  block + guide-type rules injected server-side):
    - create_shipment(payload, guide_type, confirm) : lab -> patient.
      guide_type "normal" (home) or "ocurre" (DHL branch; receiver company
      forced to "DHL OCURRE"). Shipper = pharmacy (auto).
    - create_return_shipment(payload, confirm)      : patient -> pharmacy.
      Receiver = pharmacy (auto); shipper = patient (caller-provided).

Label delivery (see project_claudeai_payload_ceiling + ADDING_AN_MCP):
  A MyDHL label PDF returned as base64 is tens of KB — far past whatever
  claude.ai can carry back through the MCP tool-result channel. Returning it
  inline HANGS the claude.ai session (confirmed in prod 2026-06-02). MyDHL
  Express returns labels ONLY as base64 (the URL-reference output is a Parcel
  DE / eCommerce feature, not Express), so there is no DHL-hosted link to hand
  back.

  So instead of inlining the base64, _externalize_documents() decodes each
  document, stashes the bytes in an in-process TTL store, and replaces
  `content` with a short `download_url`. That URL points at this container's
  GET /download/{token} route, exposed publicly (browser-openable, no MCP
  bearer) through the gateway + Worker at LABEL_DOWNLOAD_BASE_URL
  (https://mcp.choiz.com.mx/dl/dhl/<token>). The model hands the user the link;
  the PDF never transits the claude.ai tool-result channel. The store is
  in-memory only (lost on container restart) and entries expire after
  LABEL_TTL_MINUTES — labels are ephemeral; regenerate or use the MyDHL+ portal
  if a link has expired.

The MyDHL shipment body is large and account-specific (~hundreds of fields:
shipper/receiver accounts, productCode, packages, customs, value-added
services). create_shipment / create_return_shipment therefore take the body as
a passthrough `payload` dict shaped per the MyDHL API spec, rather than
re-modeling every field here. The wrapper only injects auth + required headers
and trims the response.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
import uuid
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("dhl_mcp")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# --- Config from env ------------------------------------------------------

API_KEY = os.environ["DHL_API_KEY"]
API_SECRET = os.environ["DHL_API_SECRET"]
# Default to the MyDHL TEST base. Flip to production
# (https://express.api.dhl.com/mydhlapi) deliberately in the EC2 .env once
# create-shipment payloads are validated — see the module docstring.
# NB: compose passes DHL_BASE_URL=${DHL_BASE_URL:-}, i.e. the var is present
# but EMPTY when unset. os.environ.get(..., default) does NOT fall back on an
# empty string (the key exists), so use `or` to treat "" as "use the default".
BASE_URL = (
    os.environ.get("DHL_BASE_URL") or "https://express.api.dhl.com/mydhlapi/test"
).rstrip("/")
# Optional: DHL Express account number. Auto-injected into the shipment body's
# `accounts` (as the shipper account) when the caller omits it, so the label's
# "Payer Details / Freight A/C" populates correctly.
ACCOUNT_NUMBER = os.environ.get("DHL_ACCOUNT_NUMBER", "")


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Kill-switch for the WRITE tools (create_shipment / create_return_shipment).
# Default OFF: creation is refused unless DHL_ALLOW_CREATE is explicitly truthy
# in the EC2 .env. Flip back to false + restart to instantly stop emitting
# guides (reads keep working) without a code change.
DHL_ALLOW_CREATE = _bool_env("DHL_ALLOW_CREATE", False)

_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{API_KEY}:{API_SECRET}".encode()
).decode()

# Any base64 document content longer than this gets externalized to a
# download link instead of returned inline (claude.ai payload ceiling).
_MAX_DOC_CONTENT_CHARS = 256

# Public base for browser-openable label download links. The MCP builds
# f"{LABEL_DOWNLOAD_BASE_URL}/{token}"; the gateway proxies /dl/dhl/<token> to
# this container's GET /download/<token>. Empty disables externalization
# (falls back to an omitted-content stub).
LABEL_DOWNLOAD_BASE_URL = (
    os.environ.get("LABEL_DOWNLOAD_BASE_URL") or "https://mcp.choiz.com.mx/dl/dhl"
).rstrip("/")
try:
    LABEL_TTL_MINUTES = int(os.environ.get("LABEL_TTL_MINUTES") or "30")
except ValueError:
    LABEL_TTL_MINUTES = 30
# Cap stored labels so a burst can't grow the container unbounded
# (~200 * ~100 KB ≈ 20 MB worst case). Oldest-by-expiry evicted past this.
_MAX_LABELS = 200


# --- In-memory label store (TTL, thread-safe) -----------------------------
#
# token -> {pdf: bytes, filename: str, content_type: str, expires_at: float}.
# In-process only: a container restart drops all links (acceptable — labels
# are ephemeral). Accessed from sync tool calls (anyio worker thread) and the
# async /download route, so guard with a plain Lock.

_label_lock = threading.Lock()
_label_store: dict[str, dict[str, Any]] = {}

_FORMAT_CONTENT_TYPE = {"PDF": "application/pdf", "PNG": "image/png"}


def _doc_content_type(image_format: str | None) -> str:
    return _FORMAT_CONTENT_TYPE.get((image_format or "").upper(), "application/octet-stream")


def _doc_ext(image_format: str | None) -> str:
    f = (image_format or "").lower()
    return f if f in ("pdf", "zpl", "png", "epl", "lp2") else "bin"


def _store_label(pdf: bytes, filename: str, content_type: str) -> str:
    """Stash a decoded document under a fresh unguessable token. Returns the token."""
    token = secrets.token_urlsafe(24)  # ~192 bits
    now = time.time()
    with _label_lock:
        # Purge expired, then enforce the cap by evicting the soonest-to-expire.
        for t in [t for t, e in _label_store.items() if e["expires_at"] <= now]:
            _label_store.pop(t, None)
        if len(_label_store) >= _MAX_LABELS:
            for t in sorted(_label_store, key=lambda t: _label_store[t]["expires_at"])[
                : len(_label_store) - _MAX_LABELS + 1
            ]:
                _label_store.pop(t, None)
        _label_store[token] = {
            "pdf": pdf,
            "filename": filename,
            "content_type": content_type,
            "expires_at": now + LABEL_TTL_MINUTES * 60,
        }
    return token


def _get_label(token: str) -> dict[str, Any] | None:
    """Return a non-expired stored label, or None (also drops it if expired)."""
    now = time.time()
    with _label_lock:
        entry = _label_store.get(token)
        if entry is None:
            return None
        if entry["expires_at"] <= now:
            _label_store.pop(token, None)
            return None
        return entry


# --- HTTP helper ----------------------------------------------------------


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Authenticated MyDHL API call. Raises RuntimeError on non-2xx so the LLM
    gets actionable feedback (DHL error bodies carry `detail` + `additionalDetails`).
    """
    url = f"{BASE_URL}{path}"
    headers = {
        "Authorization": _AUTH_HEADER,
        "Accept": "application/json",
    }
    if method == "POST":
        headers["Content-Type"] = "application/json"
        # MyDHL requires a unique Message-Reference (28-36 chars) on write calls.
        headers["Message-Reference"] = uuid.uuid4().hex  # 32 hex chars
    resp = requests.request(
        method, url, headers=headers, params=params, json=body, timeout=45
    )
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}

    # 2xx (including 207 multistatus on multi-piece shipments) -> success.
    if 200 <= resp.status_code < 300:
        return data

    detail = data.get("detail") or data.get("title") or data.get("message")
    extra = data.get("additionalDetails")
    raise RuntimeError(
        f"DHL API error on {method} {path} (HTTP {resp.status_code}): "
        f"{detail}" + (f" — {extra}" if extra else "")
    )


def _externalize_documents(data: dict[str, Any]) -> dict[str, Any]:
    """Replace each base64 `documents[].content` with a short download_url.

    MyDHL returns created labels/invoices under `documents[].content` as base64.
    Returned inline they hang the claude.ai session. Decode each, stash the
    bytes in the TTL store, and swap `content` for a browser-openable link
    (built from LABEL_DOWNLOAD_BASE_URL). If externalization is disabled or the
    base64 fails to decode, fall back to an omitted-content stub. Returns a
    shallow copy.
    """
    if not isinstance(data, dict):
        return data
    docs = data.get("documents")
    if not isinstance(docs, list):
        return data
    tracking = data.get("shipmentTrackingNumber") or ""
    new_docs: list[Any] = []
    for doc in docs:
        if not isinstance(doc, dict):
            new_docs.append(doc)
            continue
        d = dict(doc)
        content = d.get("content")
        if isinstance(content, str) and len(content) > _MAX_DOC_CONTENT_CHARS:
            type_code = d.get("typeCode") or "document"
            image_format = d.get("imageFormat")
            try:
                pdf_bytes = base64.b64decode(content)
            except Exception:  # malformed base64 — never inline it
                pdf_bytes = None
            if pdf_bytes and LABEL_DOWNLOAD_BASE_URL:
                filename = f"{type_code}-{tracking or 'dhl'}.{_doc_ext(image_format)}"
                token = _store_label(pdf_bytes, filename, _doc_content_type(image_format))
                d["content"] = {
                    "download_url": f"{LABEL_DOWNLOAD_BASE_URL}/{token}",
                    "expires_in_minutes": LABEL_TTL_MINUTES,
                    "filename": filename,
                }
            else:
                d["content"] = {
                    "omitted": True,
                    "base64_length": len(content),
                    "note": "Label withheld; retrieve from the MyDHL+ portal via the tracking number.",
                }
        new_docs.append(d)
    data = dict(data)
    data["documents"] = new_docs
    return data


# --- MCP server -----------------------------------------------------------

# host="0.0.0.0" so the gateway reaches us across the docker bridge.
# streamable_http_path="/" so the gateway can strip /mcp/dhl and forward to "/".
# stateless_http=True: ephemeral sessions, sidesteps stale-session-after-redeploy.
mcp = FastMCP(
    name="dhl",
    host="0.0.0.0",
    port=8080,
    streamable_http_path="/",
    stateless_http=True,
)


@mcp.custom_route("/download/{token}", methods=["GET"])
async def download_label(request: Request) -> Response:
    """Serve a stored label/document by token as a browser download.

    Reached publicly (no MCP bearer) via the gateway + Worker:
    https://mcp.choiz.com.mx/dl/dhl/<token> -> gateway /dl/dhl -> here. The
    token is the capability; unknown/expired -> 404.
    """
    entry = _get_label(request.path_params.get("token", ""))
    if entry is None:
        return Response("Label not found or expired.", status_code=404, media_type="text/plain")
    return Response(
        content=entry["pdf"],
        media_type=entry["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{entry["filename"]}"'},
    )


@mcp.tool()
def track_shipment(tracking_number: str) -> dict[str, Any]:
    """Track a DHL Express shipment by its waybill / tracking number.

    Calls GET /shipments/{tracking_number}/tracking. Returns the shipment's
    current status plus the event history (timestamp, location, status text).
    Read-only.
    """
    data = _request("GET", f"/shipments/{tracking_number}/tracking")
    # Response shape: {"shipments": [{"shipmentTrackingNumber", "status",
    #   "estimatedDeliveryDate", "events": [...], "origin", "destination"}]}
    return data


@mcp.tool()
def validate_address(
    country_code: str,
    postal_code: str = "",
    city_name: str = "",
    address_type: str = "delivery",
) -> dict[str, Any]:
    """Validate / resolve an address against DHL Express coverage.

    Calls GET /address-validate. Useful before creating a shipment to confirm
    the destination is serviceable and to get the matched city/postal code.
    `address_type` is "delivery" or "pickup". Read-only.
    """
    params: dict[str, Any] = {"type": address_type, "countryCode": country_code}
    if postal_code:
        params["postalCode"] = postal_code
    if city_name:
        params["cityName"] = city_name
    return _request("GET", "/address-validate", params=params)


@mcp.tool()
def get_rates(payload: dict[str, Any]) -> dict[str, Any]:
    """Get a rate quote for a prospective shipment (no booking, no label).

    Calls POST /rates with a MyDHL-API-shaped rate request body (customerDetails
    with shipper/receiver addresses, plannedShippingDateAndTime, accounts,
    packages). Returns available products with prices and transit times.
    Read-only — does NOT create a shipment.

    See the MyDHL API reference ("Get Rates") for the exact body schema.
    """
    return _request("POST", "/rates", body=payload)


# Guidance attached to every create response. Steers the model to hand the
# user the download link instead of trying to fetch/decode the label (which
# would pull the base64 back and hang claude.ai).
_LABEL_NOTE = (
    "Shipment created. The label is NOT inlined (a base64 label hangs claude.ai). "
    "Each entry under documents[].content.download_url is a browser link to "
    f"download the PDF, valid for {LABEL_TTL_MINUTES} minutes. Give the user that "
    "link directly — do NOT fetch or decode the label yourself."
)


# Proven outputImageProperties (validated against the MyDHL test base
# 2026-06-04): label + waybillDoc merged into ONE 2-page PDF — matches what's
# produced manually in MyDHL+. Injected when the caller omits
# outputImageProperties so the "Hand to Courier" waybill page never goes missing.
_DEFAULT_OUTPUT_IMAGE_PROPERTIES = {
    "encodingFormat": "pdf",
    "imageOptions": [
        {"typeCode": "label", "isRequested": True},
        {"typeCode": "waybillDoc", "isRequested": True},
    ],
    "allDocumentsInOneImage": True,
    "splitTransportAndWaybillDocLabels": False,
}

# Canonical pharmacy (Choiz / Farmacias Magistrales) party block. It is ALWAYS
# one side of a guide — the shipper for normal/ocurre (lab → patient), the
# receiver for returns (patient → pharmacy) — so it is injected server-side and
# every guide carries the complete, correct pharmacy details (matches MyDHL+).
_PHARMACY_DETAILS = {
    "postalAddress": {
        "postalCode": "14420",
        "cityName": "MESA DE LOS HORNOS-TLALPAN",
        "countryCode": "MX",
        "addressLine1": "La loma 20",
        "addressLine2": "Tlalpan CDMX 14420",
        "addressLine3": "Farmacias Magistrales",
    },
    "contactInformation": {
        "companyName": "Choiz",
        "fullName": "Sandra Lara",
        "phone": "+525568099093",
        "mobilePhone": "+525568099093",
        "email": "compras@farmaciasmagistrales.com.mx",
    },
}


def _apply_shipment_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    """Fill the bits the model keeps omitting, without overriding explicit input.

    - outputImageProperties: default to label + waybillDoc in one PDF (the 2nd
      "Hand to Courier" page), so the guide matches the manual MyDHL+ output.
    - accounts: inject the shipper account from DHL_ACCOUNT_NUMBER if absent, so
      "Payer Details / Freight A/C" populates.
    - pickup: MyDHL REQUIRES this key (422 "required key [pickup] not found"
      otherwise). Default to no scheduled pickup (drop-off / OCURRE flow).
    """
    if not isinstance(payload, dict):
        return payload
    p = dict(payload)
    p.setdefault("outputImageProperties", _DEFAULT_OUTPUT_IMAGE_PROPERTIES)
    p.setdefault("pickup", {"isRequested": False})
    if ACCOUNT_NUMBER and not p.get("accounts"):
        p["accounts"] = [{"typeCode": "shipper", "number": ACCOUNT_NUMBER}]
    return p


def _apply_guide_type(payload: dict[str, Any], guide_type: str) -> dict[str, Any]:
    """Place the canonical pharmacy block on the correct side + enforce per-type
    rules SERVER-SIDE (never depends on the model getting it right):

    - normal: pharmacy = shipper; receiver = patient (company name defaults to
      the patient's full name if absent).
    - ocurre: pharmacy = shipper; receiver = patient, but the receiver company
      name is FORCED to "DHL OCURRE" (else the branch won't accept the parcel).
    - return: pharmacy = RECEIVER; shipper = patient (caller-provided).
    """
    if not isinstance(payload, dict):
        return payload
    p = dict(payload)
    cd = dict(p.get("customerDetails") or {})
    if guide_type in ("normal", "ocurre"):
        cd["shipperDetails"] = _PHARMACY_DETAILS
        rcv = dict(cd.get("receiverDetails") or {})
        ci = dict(rcv.get("contactInformation") or {})
        if guide_type == "ocurre":
            ci["companyName"] = "DHL OCURRE"
        else:
            ci.setdefault("companyName", ci.get("fullName"))
        rcv["contactInformation"] = ci
        cd["receiverDetails"] = rcv
    elif guide_type == "return":
        cd["receiverDetails"] = _PHARMACY_DETAILS
    p["customerDetails"] = cd
    return p


def _confirm_summary(payload: dict[str, Any], guide_type: str) -> dict[str, Any]:
    """Human-readable digest of what will be shipped (for the confirm step)."""
    cd = payload.get("customerDetails") or {}

    def _endpoint(d: dict[str, Any] | None) -> dict[str, Any]:
        d = d or {}
        a = d.get("postalAddress") or {}
        c = d.get("contactInformation") or {}
        return {
            "name": c.get("fullName") or c.get("companyName"),
            "company": c.get("companyName"),
            "place": " ".join(
                str(x) for x in (a.get("cityName"), a.get("countryCode"), a.get("postalCode")) if x
            ),
        }

    content = payload.get("content") or {}
    accounts = payload.get("accounts") or []
    return {
        "guide_type": guide_type,
        "from": _endpoint(cd.get("shipperDetails")),
        "to": _endpoint(cd.get("receiverDetails")),
        "productCode": payload.get("productCode"),
        "declaredValue": content.get("declaredValue"),
        "currency": content.get("declaredValueCurrency"),
        "payer_account": (accounts[0].get("number") if accounts else ACCOUNT_NUMBER) or None,
    }


def _current_env() -> str:
    return "TEST" if "/test" in BASE_URL else "PRODUCTION"


def _do_create(payload: dict[str, Any], confirm: bool, *, guide_type: str) -> dict[str, Any]:
    """Shared create logic. Applies the guide-type party blocks + defaults, then
    two server-side safeguards before any DHL call: (1) the DHL_ALLOW_CREATE
    kill-switch, (2) explicit operator confirmation.
    """
    if not DHL_ALLOW_CREATE:
        return {
            "status": "disabled",
            "message": (
                "Creating guides is disabled on this MCP (DHL_ALLOW_CREATE is off). "
                "Ask ops to enable it in the EC2 .env if you need to emit guides."
            ),
        }
    final = _apply_shipment_defaults(_apply_guide_type(payload, guide_type))
    env = _current_env()
    if not confirm:
        return {
            "status": "confirmation_required",
            "guide_type": guide_type,
            "environment": (
                "PRODUCTION — emits a REAL, billable guide on the DHL account"
                if env == "PRODUCTION"
                else "TEST — sample label, no cost"
            ),
            "summary": _confirm_summary(final, guide_type),
            "message": (
                f"You are about to emit a '{guide_type}' guide in {env}. "
                + (
                    "⚠️ This creates a REAL shipment and bills the DHL account. "
                    if env == "PRODUCTION"
                    else ""
                )
                + "Review the summary with the operator; if approved, call this tool "
                "again with confirm=true to actually create the guide."
            ),
        }
    data = _request("POST", "/shipments", body=final)
    out = _externalize_documents(data)
    if isinstance(out, dict):
        out["_label_note"] = _LABEL_NOTE
    return out


@mcp.tool()
def create_shipment(
    payload: dict[str, Any], guide_type: str = "normal", confirm: bool = False
) -> dict[str, Any]:
    """Create an OUTBOUND DHL Express guide (lab → patient). WRITE OPERATION (two-step).

    `guide_type`:
      • "normal" — delivery to the patient's home address (default).
      • "ocurre" — delivery to a DHL branch for pickup; the receiver company name
        is FORCED to "DHL OCURRE" (required, or the branch rejects the parcel).

    The SHIPPER is ALWAYS the Choiz / Farmacias Magistrales pharmacy and is
    injected automatically — DO NOT provide shipperDetails. You provide the
    PATIENT as the receiver + the content.

    Safeguards: refuses unless DHL_ALLOW_CREATE is on; with confirm=false
    (default) it creates NOTHING and returns a summary to approve — re-call with
    confirm=true to actually emit. In PRODUCTION a confirmed call is real + billable.

    The label (label + "Hand to Courier" waybill, ONE 2-page PDF) comes back as
    documents[].content.download_url — give that link to the operator; never
    inlined. outputImageProperties, pickup and the payer account (986385678) are
    auto-filled. Provide:
      {
        "plannedShippingDateAndTime": "2026-06-10T13:00:00 GMT-06:00",
        "productCode": "N",                       # N = MX EXPRESS DOMESTIC
        "customerDetails": {
          "receiverDetails": {
            "postalAddress": {"postalCode","cityName","countryCode":"MX","addressLine1"},
            "contactInformation": {"fullName","phone","email"}
          }
        },
        "content": {
          "packages": [{"weight": 0.3, "dimensions": {"length":10,"width":10,"height":10}}],
          "isCustomsDeclarable": false, "unitOfMeasurement": "metric",
          "declaredValue": 1699, "declaredValueCurrency": "MXN",
          "description": "...", "incoterm": "DAP"
        }
      }
    For "ocurre" the receiver address is the DHL branch; the patient's name still
    goes in receiver fullName. Confirm declaredValue + description per shipment.
    """
    gt = (guide_type or "normal").strip().lower()
    if gt not in ("normal", "ocurre"):
        gt = "normal"
    return _do_create(payload, confirm, guide_type=gt)


@mcp.tool()
def create_return_shipment(payload: dict[str, Any], confirm: bool = False) -> dict[str, Any]:
    """Create a RETURN guide (patient → pharmacy). WRITE OPERATION (two-step).

    The RECEIVER is ALWAYS the Choiz / Farmacias Magistrales pharmacy and is
    injected automatically — DO NOT provide receiverDetails. You provide the
    PATIENT as the shipper (shipperDetails) + the content. Choiz pays the freight
    (account 986385678, auto-filled), product N.

    Same safeguards + label handling as create_shipment (confirm=true required;
    label via download_url).
    """
    return _do_create(payload, confirm, guide_type="return")


def main() -> None:
    env = "TEST" if "/test" in BASE_URL else "PRODUCTION"
    logger.info("dhl mcp starting — base=%s (%s)", BASE_URL, env)
    if env == "PRODUCTION":
        logger.warning(
            "DHL_BASE_URL points at PRODUCTION — create_shipment / "
            "create_return_shipment will book real shipments and may incur charges."
        )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
