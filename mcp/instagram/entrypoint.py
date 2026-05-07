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
  2. Mount that ``.server`` behind ``StreamableHTTPSessionManager`` (the same
     transport-layer manager FastMCP uses internally).
  3. Wrap with a Starlette app that delegates "/" to the session manager and
     enters the manager's lifespan in ``async with``.
  4. Serve with uvicorn on 0.0.0.0:8080.

No monkey-patch is needed: we construct the session manager directly and pin
the path/host/port in this file.

We replicate the fork's structlog config so log output matches what the
authors test against (the fork's main() does this before calling
InstagramMCPServer().run()).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

import structlog
import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount

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

    # stateless=False keeps SDK-level sessions across requests. This is NOT
    # the supergateway --stateful flag (which trips bug #126 under claude.ai
    # — see project_supergateway_stateful_bug126.md). The MCP SDK's session
    # manager handles claude.ai's parallel POST + GET SSE streams correctly
    # at the protocol layer; the supergateway bug was specific to
    # supergateway's bridging logic, not the spec.
    session_manager = StreamableHTTPSessionManager(
        app=instagram.server,
        event_store=None,
        json_response=False,
        stateless=False,
    )

    async def asgi_handler(scope, receive, send):  # type: ignore[no-untyped-def]
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):  # type: ignore[no-untyped-def]
        async with session_manager.run():
            yield

    # Mount at "/" — the gateway strips /mcp/<slug> before forwarding, so the
    # upstream must serve at root.
    app = Starlette(
        routes=[Mount("/", app=asgi_handler)],
        lifespan=lifespan,
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
