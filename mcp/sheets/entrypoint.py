"""Google Sheets MCP entrypoint — xing5/mcp-google-sheets served over
Streamable HTTP in-process, no supergateway.

The upstream package (`mcp_google_sheets.server`) defines a module-level
FastMCP instance ``mcp`` and, in its own ``main()``, simply calls
``mcp.run(transport=<--transport arg>)`` defaulting to stdio. We don't use
that CLI path because:

  1. We need Streamable HTTP, not stdio/SSE.
  2. The Streamable HTTP app mounts at "/mcp" by default. The gateway strips
     the ``/mcp/<name>`` prefix and forwards to the upstream at "/", so we must
     move the mount to "/" or every call 404s (same lesson as supergateway's
     --streamableHttpPath). Since mcp 2.x that is a run() kwarg, not a Settings
     field.

So we import the ready-built ``mcp`` instance and run it with the
streamable-http transport and explicit transport kwargs. Importing the module is
enough to register all @mcp.tool decorators (the package wires them at import
time) and the ``spreadsheet_lifespan`` context manager that performs Google
auth from CREDENTIALS_CONFIG. No extra init call is required.

Auth: the package reads ``CREDENTIALS_CONFIG`` (base64 of the SA JSON) and
builds ``service_account.Credentials.from_service_account_info(...)`` itself,
so we do not materialize a file. compose.yml passes
``SHEETS_SERVICE_ACCOUNT_JSON_B64`` into the container as ``CREDENTIALS_CONFIG``.
"""
from __future__ import annotations

import inspect as _inspect
import logging
import os
import sys
import types as _pytypes

from mcp.server.mcpserver import Context as _Context, MCPServer

# Stand-in for the module mcp 2.x removed. mcp-google-sheets still does
# ``from mcp.server.fastmcp import FastMCP``; 2.x renamed that class to
# MCPServer and deleted the old module (importing it now raises
# ModuleNotFoundError pointing at the migration guide). Registering the
# stand-in HERE, at our module's import time, guarantees it is in place before
# main() imports the package. mcp 2.x is not optional: protocol revision
# 2026-07-28 is unsupported by every 1.x release, which answers it with a bare
# HTTP 400 that claude.ai reports as "Connection closed" on every tool call.

# mcp-google-sheets also calls the constructor with transport kwargs
# (``FastMCP(..., host=...)`` at server.py:183). In 2.x those moved out of
# __init__ and into run()/streamable_http_app(), so passing them raises
# TypeError. Drop any kwarg the 2.x constructor does not accept — computed from
# the signature rather than hardcoded, so a future rename does not silently
# swallow something real. We pass the transport settings ourselves in main().
#
# Patched IN PLACE on the original class, never subclassed: MCPServer is a
# Generic (``MCPServer[LifespanResultT]``) and a plain subclass loses the
# generic parameterisation, breaking pydantic's forward-ref resolution at
# runtime. That is the trap documented in the warehouse entrypoint.
_ACCEPTED_KWARGS = frozenset(_inspect.signature(MCPServer.__init__).parameters)
_orig_mcpserver_init = MCPServer.__init__


def _lenient_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    dropped = [k for k in kwargs if k not in _ACCEPTED_KWARGS]
    for key in dropped:
        kwargs.pop(key)
    if dropped:
        logging.getLogger(__name__).info(
            "dropped constructor kwargs not supported by mcp 2.x: %s "
            "(transport settings are passed to run() instead)",
            ", ".join(sorted(dropped)),
        )
    _orig_mcpserver_init(self, *args, **kwargs)


MCPServer.__init__ = _lenient_init  # type: ignore[method-assign]

_shim = _pytypes.ModuleType("mcp.server.fastmcp")
_shim.FastMCP = MCPServer  # type: ignore[attr-defined]
# mcp-google-sheets imports Context from the same module for its tool
# signatures. Take it from mcp.server.mcpserver, NOT mcp.server.context: the
# tool decorator special-cases the Context parameter so it never reaches the
# input schema, and it only recognises the class exported alongside MCPServer.
# Using the mcp.server.context one makes pydantic try to build a JSON schema
# for it and blow up with PydanticInvalidForJsonSchema at import time.
_shim.Context = _Context  # type: ignore[attr-defined]
sys.modules["mcp.server.fastmcp"] = _shim


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
    # mcp 2.x exposes remove_tool() publicly; prefer it over reaching into the
    # private registry, which is exactly the kind of internal that moved when
    # FastMCP became MCPServer. Keep the private path as a fallback so this
    # still fails CLOSED (SystemExit) if neither is reachable.
    remove = getattr(mcp, "remove_tool", None)
    if callable(remove):
        removed = []
        for name in WRITE_TOOLS:
            try:
                remove(name)
                removed.append(name)
            except Exception:  # tool absent in this package version
                pass
        log.info("write gate enforced via remove_tool: %d write tools removed.",
                 len(removed))
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

    logging.getLogger(__name__).info(
        "Google Sheets MCP starting on %s:%s (streamable-http, path=/, stateless)",
        os.environ.get("HOST", "0.0.0.0"),
        os.environ.get("PORT", "8080"),
    )
    # In mcp 2.x the transport settings moved out of Settings and into explicit
    # kwargs on run(), which also removes the old "did the attribute move?"
    # guesswork this file used to guard against.
    #
    # streamable_http_path="/" because the gateway strips /mcp/sheets and
    # forwards to "/". host="0.0.0.0" so the SDK's DNS-rebinding protection does
    # not reject the "sheets_mcp:8080" Host header that http-proxy-middleware
    # (changeOrigin: true) sends.
    #
    # stateless_http=True: sessions only exist on the legacy (<= 2025-11-25)
    # transport, and keeping them costs a manual reconnect on every redeploy --
    # claude.ai keeps sending the Mcp-Session-Id of the replaced container, the
    # SDK answers an unknown session with 400, the spec says 404, and claude.ai
    # only re-initializes on 404. That wedge hit this very image on 2026-08-24.
    # See memory feedback_stale_session_after_redeploy.
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8080,
        streamable_http_path="/",
        stateless_http=True,
    )


if __name__ == "__main__":
    sys.exit(main())
