"""Warehouse MCP entrypoint, bridging postgres-mcp onto the mcp 2.x SDK.

Why this exists
---------------
MCP protocol revision 2026-07-28 removed the ``initialize`` /
``notifications/initialized`` handshake, removed protocol-level sessions and
the ``Mcp-Session-Id`` header, and made ``server/discover`` mandatory. Every
request now carries its protocol version in a ``params._meta`` envelope
(``io.modelcontextprotocol/protocolVersion``).

claude.ai started speaking it on 2026-09-11. An mcp 1.x server answers those
requests with a bare HTTP 400 (it only knows revisions up to 2025-11-25), which
the client surfaces as "Connection closed" on every tool call — while the
periodic legacy re-handshake still succeeds, so the connector keeps *looking*
healthy with its tool list intact. That is the outage this file fixes.

Support for 2026-07-28 landed only in mcp 2.x, which renamed ``FastMCP`` to
``MCPServer``; ``mcp.server.fastmcp`` now raises ModuleNotFoundError pointing at
the migration guide. postgres-mcp is still pinned to ``mcp[cli]<2.0`` upstream
(commit 15c8e333, 2026-08-16, "prevent breaking import change") and imports the
old name, so we bridge it here rather than forking a project whose last release
is v0.3.0 (May 2025).

Two patches, both applied IN PLACE on the original class — ``MCPServer`` is a
Generic (``MCPServer[LifespanResultT]``) and a plain subclass loses the generic
parameterisation, which breaks pydantic's forward-ref resolution at runtime.
That is the trap that bit the 1.x version of this file:

  1. Register a stand-in ``mcp.server.fastmcp`` module exporting
     ``FastMCP = MCPServer`` BEFORE postgres_mcp is imported, so its
     module-level ``@mcp.tool`` decorators still register.

  2. Default the transport kwargs. In 2.x host/port/path/statelessness moved
     OUT of ``Settings`` and INTO explicit keyword arguments on
     ``run_streamable_http_async``, so postgres-mcp's ``mcp.settings.host = ...``
     no longer has anywhere to land (the fields do not exist and pydantic
     raises on unknown attribute assignment). We swallow those writes and
     supply the values here.

Verified against the live warehouse RDS before deploy: ``server/discover``
returns ``supportedVersions: ["2026-07-28"]``, and ``tools/call execute_sql``
returns rows both on the modern envelope and through the gateway's rewritten
``Host: warehouse_mcp:8080`` header.

Drop this shim when postgres-mcp ships native 2.x support.
"""

from __future__ import annotations

import asyncio
import sys
import types as _pytypes

from mcp.server.mcpserver import MCPServer

# --- 1. Stand-in for the removed mcp.server.fastmcp module -------------------
_shim = _pytypes.ModuleType("mcp.server.fastmcp")
_shim.FastMCP = MCPServer  # type: ignore[attr-defined]
sys.modules["mcp.server.fastmcp"] = _shim

# --- 2. Transport defaults ---------------------------------------------------
_orig_run = MCPServer.run_streamable_http_async


async def _patched_run(self, **kwargs):  # type: ignore[no-untyped-def]
    # 2.x defaults are 127.0.0.1:8000 at path "/mcp". We need 0.0.0.0:8080 at
    # "/" so the gateway can reach the container by service name and proxy
    # without a 307 to an internal Docker hostname claude.ai cannot resolve.
    kwargs.setdefault("host", "0.0.0.0")
    kwargs.setdefault("port", 8080)
    kwargs.setdefault("streamable_http_path", "/")
    # Sessions only exist on the legacy (<= 2025-11-25) transport, which older
    # clients still use. Keeping it stateless costs a manual reconnect on every
    # redeploy otherwise: the client keeps sending the Mcp-Session-Id of the
    # replaced container, the SDK answers an unknown session with 400, the spec
    # says 404, and claude.ai only re-initializes on 404 — so the connector
    # stays wedged. See memory feedback_stale_session_after_redeploy.
    kwargs.setdefault("stateless_http", True)
    return await _orig_run(self, **kwargs)


MCPServer.run_streamable_http_async = _patched_run  # type: ignore[method-assign]

# postgres-mcp writes mcp.settings.host / .port before calling the runner.
# Those fields are gone in 2.x; swallow the writes (values come from
# _patched_run above) instead of letting pydantic raise at startup.
_SettingsType = type(MCPServer(name="_probe").settings)
_orig_setattr = _SettingsType.__setattr__


def _lenient_setattr(self, name, value):  # type: ignore[no-untyped-def]
    if name in {"host", "port"}:
        return
    _orig_setattr(self, name, value)


_SettingsType.__setattr__ = _lenient_setattr  # type: ignore[method-assign]

# Import AFTER the shim is in place: postgres_mcp.server resolves
# ``from mcp.server.fastmcp import FastMCP`` at module load time.
from postgres_mcp.server import main  # noqa: E402


if __name__ == "__main__":
    asyncio.run(main())
