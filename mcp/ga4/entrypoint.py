"""GA4 MCP entrypoint — official googleanalytics/google-analytics-mcp wrapped
in streamable-http transport.

Cutover from the previous Choiz fork (Choizapp/choiz-ga4-mcp@a0278ff) to the
**official** Google Analytics MCP at
https://github.com/googleanalytics/google-analytics-mcp pinned to commit
6fd3bf88. The official server uses ``mcp.server.lowlevel.Server`` and a
hardcoded ``stdio_server()`` transport — the same shape as our instagram
fork — so the migration shape is the same:

  1. Decode ``GA4_SERVICE_ACCOUNT_JSON_B64`` from env (single Choiz reader
     SA, shared across tenants) into a JSON file at /tmp/sa.json. Set
     ``GOOGLE_APPLICATION_CREDENTIALS`` to that path so the
     google-analytics-data + google-analytics-admin clients pick it up via
     Application Default Credentials. The base64 form survives the
     newline-sensitive .env loader.
  2. Import ``analytics_mcp.server`` to register all @app.tool decorators
     (run_report, run_funnel_report, get_account_summaries, etc.) on the
     module-level lowlevel Server at ``analytics_mcp.coordinator.app``.
  3. Ask that Server for a Starlette app via ``streamable_http_app()`` and
     serve it under uvicorn on 0.0.0.0:8080.

Since mcp 2.x (required for protocol revision 2026-07-28 — see the Dockerfile)
step 3 is a single call: the low-level Server grows a ``streamable_http_app()``
that wires the session manager, its lifespan AND the modern per-request
transport. Hand-rolling StreamableHTTPSessionManager the way this file used to
only gets you the legacy transport, so the server would still answer the
current protocol with HTTP 400.

property_id is now a per-tool argument, not a per-container env. Two
containers still exist (ga4_choiz_mcp + ga4_timeless_mcp) for slug
separation; both run the same code with the same auth and just expose
different routes for connector clarity. The model passes the right
property_id per call.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys

import uvicorn


def _materialize_service_account() -> None:
    """Decode the base64 SA JSON env into a file the google-auth lib reads.

    Carries forward the pattern from the previous Choiz fork's entrypoint —
    this is the cleanest way to inject auth into the host google-auth ADC
    chain without checking JSON files into the repo.
    """
    b64 = os.environ.get("GA4_SERVICE_ACCOUNT_JSON_B64")
    if not b64:
        raise RuntimeError(
            "GA4_SERVICE_ACCOUNT_JSON_B64 env var is required (base64-encoded "
            "Google service account JSON for the Choiz GA4 reader account)."
        )
    sa_path = "/tmp/ga4-sa.json"
    with open(sa_path, "wb") as f:
        f.write(base64.b64decode(b64))
    os.chmod(sa_path, 0o600)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_path


async def _serve() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    _materialize_service_account()

    # Importing analytics_mcp.server runs ``from .coordinator import ...``
    # which in turn imports all tool modules; their @app.tool decorators
    # register against the module-level Server. After this import,
    # analytics_mcp.coordinator.app is fully populated.
    import analytics_mcp.server  # noqa: F401
    from analytics_mcp.coordinator import app

    # stateless_http=True: every request is its own ephemeral session.
    #
    # This was stateless=False, and 2026-08-24 showed the cost. After the
    # container was replaced by a redeploy, claude.ai kept sending the
    # Mcp-Session-Id of the container that no longer existed; the Python SDK
    # answers an unknown session with 400, the MCP spec says 404, and claude.ai
    # only re-initializes on 404. Net effect: the connector is wedged until a
    # human reconnects it by hand — on every single redeploy. See memory
    # feedback_stale_session_after_redeploy. Sessions only exist on the legacy
    # (<= 2025-11-25) transport at all, so this now only affects older clients.
    #
    # streamable_http_path="/" because the gateway strips /mcp/ga4-<brand> and
    # forwards to "/". host="0.0.0.0" so the SDK's DNS-rebinding protection
    # does not reject the "ga4_<brand>_mcp:8080" Host header that
    # http-proxy-middleware (changeOrigin: true) sends.
    starlette_app = app.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        host="0.0.0.0",
    )

    config = uvicorn.Config(
        starlette_app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(_serve())
