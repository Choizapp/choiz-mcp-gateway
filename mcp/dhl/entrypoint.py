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
  write:
    - create_shipment(payload)               : POST /shipments  -> tracking + label
    - create_return_shipment(payload)        : POST /shipments (return product) -> tracking + label

Payload-ceiling discipline (see project_claudeai_payload_ceiling + ADDING_AN_MCP):
  A MyDHL label PDF returned as base64 is tens of KB — far past claude.ai's
  ~2-3 KB tool-result ceiling. _trim_documents() strips the base64 `content`
  by default, returning tracking number + document metadata (typeCode, format,
  base64 length) instead. Pass include_label_content=True to force the raw
  base64 back (works via curl/direct API; will likely fail through claude.ai).
  Prefer requesting URL-referenced labels in your payload's
  outputImageProperties so the wrapper returns a short link instead.

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
import uuid
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

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
BASE_URL = os.environ.get(
    "DHL_BASE_URL", "https://express.api.dhl.com/mydhlapi/test"
).rstrip("/")
# Optional: DHL Express account number. Most MyDHL calls carry the account in
# the request body (`accounts`), so this is only used as a convenience default
# the caller can reference; it is NOT auto-injected into payloads.
ACCOUNT_NUMBER = os.environ.get("DHL_ACCOUNT_NUMBER", "")

_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{API_KEY}:{API_SECRET}".encode()
).decode()

# Truncate any base64 document content longer than this before returning it
# to the model, to stay under claude.ai's payload ceiling.
_MAX_DOC_CONTENT_CHARS = 256


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


def _trim_documents(data: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
    """Strip heavy base64 label content out of a shipment response.

    MyDHL returns created labels/invoices under `documents[].content` as base64.
    Left inline these blow past claude.ai's payload ceiling. Unless the caller
    opts in with include_content=True, replace each `content` with a small
    metadata stub. Mutates a shallow copy and returns it.
    """
    if not isinstance(data, dict):
        return data
    docs = data.get("documents")
    if isinstance(docs, list):
        trimmed = []
        for doc in docs:
            if not isinstance(doc, dict):
                trimmed.append(doc)
                continue
            d = dict(doc)
            content = d.get("content")
            if (
                isinstance(content, str)
                and len(content) > _MAX_DOC_CONTENT_CHARS
                and not include_content
            ):
                d["content"] = {
                    "omitted": True,
                    "base64_length": len(content),
                    "note": (
                        "Label base64 omitted to stay under claude.ai's payload "
                        "ceiling. Retrieve via direct API with include_label_content=True, "
                        "or request URL-referenced output in outputImageProperties."
                    ),
                }
            trimmed.append(d)
        data = dict(data)
        data["documents"] = trimmed
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


@mcp.tool()
def create_shipment(
    payload: dict[str, Any],
    include_label_content: bool = False,
) -> dict[str, Any]:
    """Create a DHL Express shipment and generate its label. WRITE OPERATION.

    Calls POST /shipments with a full MyDHL-API shipment body (passthrough).
    On success returns the assigned tracking number(s) and document metadata.

    IMPORTANT — this books a real shipment on the DHL account and may incur
    charges. It hits whatever DHL_BASE_URL points at; that defaults to the
    MyDHL TEST environment. Production only when DHL_BASE_URL is set to the
    production base in the EC2 .env.

    The `payload` must follow the MyDHL API "Create Shipment" schema, e.g.:
      {
        "plannedShippingDateAndTime": "2026-06-10T13:00:00 GMT+00:00",
        "productCode": "P",                # DHL Express product (e.g. P = Worldwide)
        "accounts": [{"typeCode": "shipper", "number": "<DHL_ACCOUNT_NUMBER>"}],
        "customerDetails": {"shipperDetails": {...}, "receiverDetails": {...}},
        "content": {"packages": [...], "isCustomsDeclarable": false, ...},
        "outputImageProperties": {...}     # request URL output here to avoid base64
      }

    Label handling: MyDHL returns labels under documents[].content as base64.
    That is stripped by default (payload ceiling). Set include_label_content=True
    to force the raw base64 back — works via direct API/curl but will likely
    exceed claude.ai's tool-result ceiling. Prefer requesting URL-referenced
    documents in outputImageProperties.
    """
    data = _request("POST", "/shipments", body=payload)
    return _trim_documents(data, include_content=include_label_content)


@mcp.tool()
def create_return_shipment(
    payload: dict[str, Any],
    include_label_content: bool = False,
) -> dict[str, Any]:
    """Create a DHL Express RETURN shipment + return label. WRITE OPERATION.

    Same endpoint as create_shipment (POST /shipments) — a return is modeled in
    the body by using your account's return product code and swapping shipper /
    receiver so the parcel routes back to you. Configure the return per your DHL
    Express account (paperless return vs printed return label) in `payload`.

    Same billing + TEST/production caveats and same base64 label-stripping
    behaviour as create_shipment. See the MyDHL API reference for the return
    body schema.
    """
    data = _request("POST", "/shipments", body=payload)
    return _trim_documents(data, include_content=include_label_content)


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
