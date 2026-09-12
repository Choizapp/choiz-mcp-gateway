"""Instagram MCP entrypoint — serves the lowlevel Server via streamable-http.

The fork (Choizapp/choiz-instagram-mcp) uses ``mcp.server.lowlevel.Server``
directly — NOT FastMCP. ``InstagramMCPServer.run()`` hardcodes a
``stdio_server()`` context manager. The previous image worked around that by
wrapping the stdio server in supergateway, which spawns a fresh Python child
per request and never reaps them; sustained load grew this container to ~2 GB
(observed 2026-05-07 pre-fix). This is the same leak we removed from
warehouse and facebook.

Migration shape (different from warehouse/facebook because there is no
FastMCP to monkey-patch):

  1. Instantiate ``InstagramMCPServer()`` — this registers all tools on its
     internal ``.server`` (a ``mcp.server.lowlevel.Server`` instance).
  2. Ask that ``.server`` for a Starlette app via ``streamable_http_app()``
     and serve it with uvicorn on 0.0.0.0:8080.

Since mcp 2.x (required for protocol revision 2026-07-28 — see the Dockerfile)
step 2 is a single call: the low-level Server grows a ``streamable_http_app()``
that wires the session manager, its lifespan AND the modern per-request
transport. Hand-rolling ``StreamableHTTPSessionManager`` the way this file used
to only gets you the legacy transport, so the server would still answer the
current protocol with HTTP 400.

No monkey-patch is needed: we ask the server for its app and pin the
path/host/statelessness in this file.

We replicate the fork's structlog config so log output matches what the
authors test against (the fork's main() does this before calling
InstagramMCPServer().run()).
"""
from __future__ import annotations

import asyncio
import logging
import sys

import structlog
import uvicorn

# Make /app importable so `src` is found as a package. We must NOT add
# /app/src to sys.path — that would import instagram_mcp_server as a
# top-level module, breaking its `from .config import get_settings`
# relative import (ImportError: attempted relative import with no known
# parent package). The fork's previous launcher `python -m src.instagram_mcp_server`
# preserved the package context the same way.
sys.path.insert(0, "/app")

# Imported for the side effect of building the InstagramMCPServer class.
from src.instagram_mcp_server import InstagramMCPServer, get_settings  # noqa: E402


def _configure_logging() -> None:
    """Mirror the fork's main() logging setup so output stays compatible."""
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level))


async def _serve() -> None:
    _configure_logging()

    instagram = InstagramMCPServer()  # registers tools on instagram.server

    # stateless_http=True. This was stateless=False, which kept SDK-level
    # sessions across requests; sessions only exist on the legacy
    # (<= 2025-11-25) transport at all, and keeping them costs a manual
    # reconnect on every redeploy — claude.ai keeps sending the Mcp-Session-Id
    # of the replaced container, the SDK answers an unknown session with 400,
    # the spec says 404, and claude.ai only re-initializes on 404. That wedge
    # hit ga4/sheets on 2026-08-24; see feedback_stale_session_after_redeploy.
    #
    # streamable_http_path="/" because the gateway strips /mcp/<slug> before
    # forwarding, so the upstream must serve at root. host="0.0.0.0" so the
    # SDK's DNS-rebinding protection does not reject the
    # "instagram_<tenant>_mcp:8080" Host header that http-proxy-middleware
    # (changeOrigin: true) sends.
    app = instagram.server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        host="0.0.0.0",
    )

    # Host 0.0.0.0 so docker bridge networking works; port 8080 matches the
    # gateway's UPSTREAM_INSTAGRAM_* URLs.
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(_serve())
