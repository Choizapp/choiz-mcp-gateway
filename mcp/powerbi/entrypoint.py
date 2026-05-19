"""Power BI MCP — custom server exposing read-only access to PBI semantic models.

End users authenticate to claude.ai with Google Workspace. The gateway routes
their request here; this container talks to Power BI Service using a Service
Principal (client_credentials), so users never see a Microsoft login prompt.

Tool surface (read-only):
  - list_datasets()                   : datasets available through this MCP
  - describe_dataset(dataset)         : tables + descriptions
  - list_measures(dataset, table?)    : DAX measures
  - list_columns(dataset, table)      : columns of a table
  - list_relationships(dataset)       : relationships between tables
  - query_dax(dataset, dax)           : execute arbitrary DAX

Discovery (describe_dataset / list_measures / list_columns / list_relationships)
runs DAX INFO.VIEW.* functions through executeQueries — empirically supported
on PPU as of 2026-05-19, despite an older docs snippet claiming otherwise.

Token caching is handled in-process by msal:
  ``ConfidentialClientApplication.acquire_token_for_client`` returns cached
  tokens until ~5 min before expiry and silently re-mints, so each tool
  invocation just calls it without thinking about refresh.

Multi-dataset by slug: PBI_DATASET_CHOIZ + PBI_DATASET_TIMELESS map to the
"choiz" / "timeless" arg on each tool. The set is closed (validated at startup),
so the model can't accidentally point at a workspace it shouldn't.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import msal
import requests
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("powerbi_mcp")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

# --- Config from env (all required) ---------------------------------------

TENANT_ID = os.environ["PBI_TENANT_ID"]
CLIENT_ID = os.environ["PBI_CLIENT_ID"]
CLIENT_SECRET = os.environ["PBI_CLIENT_SECRET"]
WORKSPACE_ID = os.environ["PBI_WORKSPACE_ID"]

# Closed slug → dataset GUID mapping. Add a new line + new env var to expose
# another model; do NOT let users pass arbitrary dataset_ids.
DATASETS: dict[str, str] = {
    "choiz":    os.environ["PBI_DATASET_CHOIZ"],
    "timeless": os.environ["PBI_DATASET_TIMELESS"],
}

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]
PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

# Default cap on rows the model can pull in one shot via query_dax. The PBI
# REST endpoint allows 100K, but a 100K-row response will blow past the
# claude.ai payload ceiling and waste context. 1000 is a sane default; the
# model can override per-call if needed.
DEFAULT_TOP_DEFAULT = 1000

# Single msal app — keeps the token cache across calls.
_msal_app = msal.ConfidentialClientApplication(
    CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET
)


def _resolve_dataset(dataset: str) -> str:
    """Map slug → GUID, with a useful error if the slug is unknown."""
    try:
        return DATASETS[dataset]
    except KeyError:
        valid = ", ".join(sorted(DATASETS))
        raise ValueError(
            f"unknown dataset {dataset!r}. Valid datasets: {valid}"
        ) from None


def _token() -> str:
    """Acquire (or reuse cached) bearer token for the Power BI scope."""
    result = _msal_app.acquire_token_for_client(scopes=SCOPE)
    if "access_token" not in result:
        raise RuntimeError(
            f"failed to acquire Power BI token: "
            f"{result.get('error')} — {result.get('error_description')}"
        )
    return result["access_token"]


def _strip_brackets(row: dict[str, Any]) -> dict[str, Any]:
    """Power BI returns column keys as '[Name]'. Strip the brackets so the
    LLM sees clean dict keys it can reason about."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key.startswith("[") and key.endswith("]"):
            out[key[1:-1]] = value
        else:
            out[key] = value
    return out


def _execute_query(dataset_id: str, dax: str) -> list[dict[str, Any]]:
    """POST a DAX query to executeQueries and return cleaned rows.

    Raises RuntimeError with the PBI error body on non-2xx so the LLM gets
    actionable feedback (bad table name, RLS block, etc).
    """
    url = (
        f"{PBI_BASE}/groups/{WORKSPACE_ID}/datasets/{dataset_id}/executeQueries"
    )
    body = {
        "queries": [{"query": dax}],
        "serializerSettings": {"includeNulls": True},
    }
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    if r.status_code != 200:
        # Include both status and (truncated) body so the LLM can self-correct
        # on common errors (e.g. column reference typos return a clear message).
        raise RuntimeError(
            f"executeQueries HTTP {r.status_code}: {r.text[:600]}"
        )
    payload = r.json()
    rows = payload["results"][0]["tables"][0]["rows"]
    return [_strip_brackets(row) for row in rows]


# --- MCP server -----------------------------------------------------------

# host="0.0.0.0" so the gateway can reach us across the docker bridge with
#   Host: powerbi_mcp:8080 (changeOrigin: true in http-proxy-middleware).
#   FastMCP otherwise auto-enables DNS rebinding protection that rejects
#   non-localhost Host headers — same gotcha as warehouse.
# streamable_http_path="/" so the gateway can strip /mcp/powerbi and forward
#   to "/". FastMCP's default is "/mcp" which would force the gateway to
#   forward /mcp instead of /, leaking the internal path into errors.
mcp = FastMCP(
    name="powerbi",
    host="0.0.0.0",
    port=8080,
    streamable_http_path="/",
)


@mcp.tool()
def list_datasets() -> list[dict[str, str]]:
    """List the Power BI semantic models exposed through this MCP.

    Returns one entry per dataset with `slug` (the value to pass to other
    tools' `dataset` argument), `name`, and `dataset_id` (the underlying
    Power BI dataset GUID, for reference).

    Use this first if you are unsure which datasets are available. After
    that, call `describe_dataset(dataset=<slug>)` to learn the table layout.
    """
    return [
        {"slug": slug, "dataset_id": guid, "workspace_id": WORKSPACE_ID}
        for slug, guid in DATASETS.items()
    ]


@mcp.tool()
def describe_dataset(dataset: str) -> dict[str, Any]:
    """List all visible tables in a Power BI dataset, with their descriptions.

    The model authors documented each table in the semantic model itself
    (Description property). This tool surfaces that documentation so the
    LLM can pick the right table before writing DAX.

    Args:
      dataset: Slug from `list_datasets()` (e.g. "choiz" or "timeless").

    Returns:
      dict with `dataset` (echoed slug) and `tables` — a list of
      {name, description, is_hidden, data_category, calculation_group_precedence}.
      Hidden tables are returned but flagged; the LLM should generally avoid
      querying them directly.
    """
    dataset_id = _resolve_dataset(dataset)
    rows = _execute_query(dataset_id, "EVALUATE INFO.VIEW.TABLES()")
    tables = [
        {
            "name": r.get("Name"),
            "description": r.get("Description"),
            "is_hidden": r.get("IsHidden"),
            "data_category": r.get("DataCategory"),
            "calculation_group_precedence": r.get("CalculationGroupPrecedence"),
        }
        for r in rows
    ]
    return {"dataset": dataset, "tables": tables}


@mcp.tool()
def list_measures(dataset: str, table: str | None = None) -> list[dict[str, Any]]:
    """List DAX measures defined in a Power BI dataset.

    Args:
      dataset: Slug from `list_datasets()` (e.g. "choiz" or "timeless").
      table:   Optional. If provided, returns only measures whose home table
               matches this name (case-sensitive, as stored in the model).

    Returns:
      List of {name, table, description, display_folder, expression, is_hidden}.
      `expression` is the DAX body of the measure — useful when the LLM
      needs to compose a derived calculation or understand semantics.
    """
    dataset_id = _resolve_dataset(dataset)
    rows = _execute_query(dataset_id, "EVALUATE INFO.VIEW.MEASURES()")
    measures = [
        {
            "name": r.get("Name"),
            "table": r.get("Table"),
            "description": r.get("Description"),
            "display_folder": r.get("DisplayFolder"),
            "expression": r.get("Expression"),
            "is_hidden": r.get("IsHidden"),
        }
        for r in rows
    ]
    if table is not None:
        measures = [m for m in measures if m["table"] == table]
    return measures


@mcp.tool()
def list_columns(dataset: str, table: str) -> list[dict[str, Any]]:
    """List columns of a specific table in a Power BI dataset.

    Args:
      dataset: Slug from `list_datasets()`.
      table:   Table name (case-sensitive, exactly as returned by
               `describe_dataset`).

    Returns:
      List of {name, table, data_type, is_hidden, is_key, description,
      display_folder}.
    """
    dataset_id = _resolve_dataset(dataset)
    rows = _execute_query(dataset_id, "EVALUATE INFO.VIEW.COLUMNS()")
    columns = [
        {
            "name": r.get("Name"),
            "table": r.get("Table"),
            "data_type": r.get("DataType"),
            "is_hidden": r.get("IsHidden"),
            "is_key": r.get("IsKey"),
            "description": r.get("Description"),
            "display_folder": r.get("DisplayFolder"),
        }
        for r in rows
        if r.get("Table") == table
    ]
    return columns


@mcp.tool()
def list_relationships(dataset: str) -> list[dict[str, Any]]:
    """List relationships between tables in a Power BI dataset.

    Useful for understanding which tables can be joined implicitly via
    DAX and which direction the cross-filter flows.

    Args:
      dataset: Slug from `list_datasets()`.

    Returns:
      List of {from_table, from_column, to_table, to_column, is_active,
      cross_filtering_behavior, relationship_type}.
    """
    dataset_id = _resolve_dataset(dataset)
    rows = _execute_query(dataset_id, "EVALUATE INFO.VIEW.RELATIONSHIPS()")
    return [
        {
            "from_table": r.get("FromTable"),
            "from_column": r.get("FromColumn"),
            "to_table": r.get("ToTable"),
            "to_column": r.get("ToColumn"),
            "is_active": r.get("IsActive"),
            "cross_filtering_behavior": r.get("CrossFilteringBehavior"),
            "relationship_type": r.get("RelationshipType"),
        }
        for r in rows
    ]


@mcp.tool()
def query_dax(dataset: str, dax: str) -> dict[str, Any]:
    """Execute a DAX query against a Power BI semantic model.

    The query MUST be a valid DAX statement starting with EVALUATE.
    Prefer measures defined in the model (`list_measures`) over inlining
    aggregations — they encode the team's business logic and stay
    consistent with the official PBI reports.

    Hard limits enforced by the Power BI REST API:
      * 100,000 rows per query
      * 1,000,000 values total (rows × columns)
      * 120 queries / minute / dataset
    Use TOPN(...) or SUMMARIZECOLUMNS(...) with TREATAS/FILTER to constrain
    big tables before they hit the wire.

    Args:
      dataset: Slug from `list_datasets()`.
      dax:     DAX query, e.g.
               `EVALUATE TOPN(10, 'Orders')`
               `EVALUATE SUMMARIZECOLUMNS('Date'[Year], "Revenue", [Total Revenue])`
               `EVALUATE FILTER('Customers', 'Customers'[Country] = "MX")`

    Returns:
      dict with `rows` (list of {column_name: value}, brackets stripped) and
      `row_count`. Errors from the PBI engine (bad column refs, RLS blocks,
      etc.) are surfaced as exceptions with the PBI error body included so
      you can self-correct.
    """
    dataset_id = _resolve_dataset(dataset)
    rows = _execute_query(dataset_id, dax)
    return {"rows": rows, "row_count": len(rows)}


def main() -> None:
    # Smoke-validate config at startup so a misconfigured container fails
    # fast in `docker logs` instead of erroring on the first user query.
    logger.info(
        "powerbi mcp starting — workspace=%s, datasets=%s",
        WORKSPACE_ID,
        ", ".join(DATASETS),
    )
    try:
        _token()
        logger.info("initial token acquisition OK")
    except Exception as exc:  # pragma: no cover — startup probe
        logger.error("initial token acquisition FAILED: %s", exc)
        # Don't exit: token mint may fail transiently at boot but recover. We
        # log loudly and let mcp.run() proceed; first user request will retry.

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
