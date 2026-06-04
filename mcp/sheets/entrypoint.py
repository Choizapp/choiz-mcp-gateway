"""Google Sheets MCP entrypoint — xing5/mcp-google-sheets served over
Streamable HTTP in-process, no supergateway.

The upstream package (`mcp_google_sheets.server`) defines a module-level
FastMCP instance ``mcp`` and, in its own ``main()``, simply calls
``mcp.run(transport=<--transport arg>)`` defaulting to stdio. We don't use
that CLI path because:

  1. We need Streamable HTTP, not stdio/SSE.
  2. FastMCP's Streamable HTTP app mounts at ``settings.streamable_http_path``
     which defaults to "/mcp". The gateway strips the ``/mcp/<name>`` prefix
     and forwards to the upstream at "/", so we must move the mount to "/"
     or every call 404s (same lesson as supergateway's --streamableHttpPath).

So we import the ready-built ``mcp`` instance, override the mount path to
"/", and run it with the streamable-http transport. Importing the module is
enough to register all @mcp.tool decorators (the package wires them at import
time) and the ``spreadsheet_lifespan`` context manager that performs Google
auth from CREDENTIALS_CONFIG. No extra init call is required.

Auth: the package reads ``CREDENTIALS_CONFIG`` (base64 of the SA JSON) and
builds ``service_account.Credentials.from_service_account_info(...)`` itself,
so we do not materialize a file. compose.yml passes
``SHEETS_SERVICE_ACCOUNT_JSON_B64`` into the container as ``CREDENTIALS_CONFIG``.
"""
from __future__ import annotations

import logging
import os
import sys


# Tool classification for mcp-google-sheets 0.6.3 (verified against the
# installed package's tool registry — 20 tools total). READ_ONLY are the
# get_/list_/search_/find_ tools that never mutate. WRITE_TOOLS mutate cell
# values, structure, or sharing.
READ_ONLY_TOOLS = (
    "get_sheet_data",
    "get_sheet_formulas",
    "get_multiple_sheet_data",
    "get_multiple_spreadsheet_summary",
    "list_spreadsheets",
    "list_sheets",
    "list_folders",
    "search_spreadsheets",
    "find_in_spreadsheet",
)
WRITE_TOOLS = (
    "create_spreadsheet",
    "create_sheet",
    "update_cells",
    "batch_update_cells",
    "batch_update",
    "add_rows",
    "add_columns",
    "copy_sheet",
    "rename_sheet",
    "share_spreadsheet",
    "add_chart",
)


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def _writes_allowed() -> bool:
    return _truthy(os.environ.get("SHEETS_ALLOW_WRITE"))


def _apply_write_gate() -> None:
    """Optimization (pre-import): pin ENABLED_TOOLS to the read-only subset so
    the package never even registers write tools. The package reads
    ENABLED_TOOLS at import time. This is best-effort — correctness is
    guaranteed by _enforce_write_gate() after import, not here — because
    0.5.x silently ignored ENABLED_TOOLS. Skips if writes are allowed or an
    operator set ENABLED_TOOLS explicitly.
    """
    log = logging.getLogger(__name__)
    if _writes_allowed() or os.environ.get("ENABLED_TOOLS"):
        return
    os.environ["ENABLED_TOOLS"] = ",".join(READ_ONLY_TOOLS)
    log.info("SHEETS_ALLOW_WRITE off — requesting read-only subset (%d tools).",
             len(READ_ONLY_TOOLS))


def _enforce_write_gate(mcp) -> None:
    """Authoritative kill-switch (post-import): when writes are not allowed,
    physically drop every write tool from the registry. The kill-switch wins
    over any ENABLED_TOOLS allowlist — SHEETS_ALLOW_WRITE off means no write
    tool is reachable, full stop. Independent of whether the package honored
    ENABLED_TOOLS, so a future version regressing that feature cannot fail
    open.
    """
    log = logging.getLogger(__name__)
    if _writes_allowed():
        log.warning("SHEETS_ALLOW_WRITE is on — write tools ENABLED.")
        return
    try:
        registry = mcp._tool_manager._tools  # noqa: SLF001 - intentional
    except AttributeError:
        log.error("cannot reach tool registry to enforce write gate — "
                  "refusing to start rather than expose writes.")
        raise SystemExit(1)
    removed = [name for name in WRITE_TOOLS if registry.pop(name, None) is not None]
    log.info("write gate enforced: %d write tools removed, %d tools remain.",
             len(removed), len(registry))


def _check_credentials() -> None:
    """Fail fast with a clear message if the SA base64 env is missing.

    The package itself would fall back to ADC/OAuth and produce a confusing
    error deep in the lifespan; surfacing it here keeps `docker logs` legible.
    """
    if not os.environ.get("CREDENTIALS_CONFIG"):
        raise RuntimeError(
            "CREDENTIALS_CONFIG env var is required (base64-encoded Google "
            "service account JSON for the sheets-editor account). compose.yml "
            "maps SHEETS_SERVICE_ACCOUNT_JSON_B64 -> CREDENTIALS_CONFIG."
        )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _check_credentials()
    _apply_write_gate()

    # Importing the package builds the FastMCP `mcp` instance and registers
    # every tool + the auth lifespan. _apply_write_gate() must precede this:
    # the package reads ENABLED_TOOLS at import time.
    from mcp_google_sheets.server import mcp

    # Authoritative kill-switch — runs after registration so it cannot be
    # bypassed by a version that ignores ENABLED_TOOLS.
    _enforce_write_gate(mcp)

    # FastMCP defaults to "/mcp"; the gateway forwards to "/". Override before
    # serving. Belt-and-suspenders: set it on settings (used to build the ASGI
    # app) regardless of the SDK minor version's attribute layout.
    try:
        mcp.settings.streamable_http_path = "/"
    except AttributeError:  # pragma: no cover - SDK layout drift guard
        logging.getLogger(__name__).warning(
            "could not set streamable_http_path on mcp.settings; "
            "relying on FASTMCP_STREAMABLE_HTTP_PATH env instead"
        )

    logging.getLogger(__name__).info(
        "Google Sheets MCP starting on %s:%s (streamable-http, path=/)",
        os.environ.get("HOST", "0.0.0.0"),
        os.environ.get("PORT", "8080"),
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    sys.exit(main())
