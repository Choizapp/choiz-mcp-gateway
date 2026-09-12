"""Facebook MCP entrypoint — serves the fork's MCP server via streamable-http.

Why this exists
---------------
MCP protocol revision 2026-07-28 (which claude.ai switched to on 2026-09-11)
removed the ``initialize`` handshake and protocol-level sessions, and made
``server/discover`` mandatory. Support landed only in the mcp 2.x SDK, which
renamed ``FastMCP`` to ``MCPServer``; ``mcp.server.fastmcp`` now raises
ModuleNotFoundError pointing at the migration guide.

The fork's ``server.py`` still does ``from mcp.server.fastmcp import FastMCP``
and ``mcp = FastMCP("FacebookMCP")``, registering tools with ``@mcp.tool()``;
it never calls ``mcp.run()``. So we register a stand-in module under that name
BEFORE importing it, exporting ``FastMCP = MCPServer``, and drive the transport
ourselves.

This replaces the old ``FastMCP.__init__`` monkey-patch: in 2.x the transport
settings (host / port / path / statelessness) moved OUT of the constructor and
INTO explicit keyword arguments on ``run()``, so there is nothing left to patch
into __init__ — we just pass them below.

Drop the shim when the fork imports ``MCPServer`` natively.
"""

from __future__ import annotations

import sys
import types as _pytypes

from mcp.server.mcpserver import MCPServer

# Stand-in for the module mcp 2.x removed. Must be registered BEFORE the fork
# is imported: its module body resolves the old name at load time.
_shim = _pytypes.ModuleType("mcp.server.fastmcp")
_shim.FastMCP = MCPServer  # type: ignore[attr-defined]
sys.modules["mcp.server.fastmcp"] = _shim

# /app holds the cloned fork; make it importable regardless of CWD.
sys.path.insert(0, "/app")

# Import for side effect: builds the server instance and registers tools.
from server import mcp  # noqa: E402


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        # Path "/" so the gateway (which strips /mcp/<slug>) reaches the server
        # without a 307 redirect to an internal Docker hostname.
        streamable_http_path="/",
        # Host 0.0.0.0 disables the SDK's auto DNS-rebinding protection, which
        # otherwise rejects the "facebook_<tenant>_mcp:8080" Host header that
        # http-proxy-middleware (changeOrigin: true) sends -> HTTP 421.
        host="0.0.0.0",
        # Listen on 8080 to match the gateway's UPSTREAM_FACEBOOK_* URLs.
        port=8080,
        # Sessions only exist on the legacy (<= 2025-11-25) transport that older
        # clients still use. Keeping it stateless sidesteps the stale
        # Mcp-Session-Id-after-redeploy wedge that bit ga4/sheets on 2026-08-24
        # (feedback_stale_session_after_redeploy): the SDK answers an unknown
        # session with 400, the spec says 404, and claude.ai only re-initializes
        # on 404. This image was previously stateful and had the same exposure.
        stateless_http=True,
    )
