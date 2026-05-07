"""Google Ads MCP entrypoint — official googleads/google-ads-mcp wrapped in
streamable-http with static creds.

Cutover from the previous Choiz fork (Choizapp/choiz-google-ads-mcp@6fefe68,
disabled 2026-04-28 due to tunnel-zombie pattern under supergateway --stateless)
to the official MCP at https://github.com/googleads/google-ads-mcp pinned to
commit 2a09ac31.

Key architectural points:

  - The official MCP defines ``mcp = FastMCP("Google Ads Server")`` at
    ``ads_mcp.coordinator``, where ``FastMCP`` comes from the
    ``fastmcp>=3.2`` package (gofastmcp.com), NOT from
    ``mcp.server.fastmcp`` (which is the official MCP SDK's
    bundled FastMCP and is a different class). The two have similar
    APIs but distinct module paths and slightly different .run()
    kwargs.
  - With OAuth env vars (``GOOGLE_ADS_MCP_OAUTH_*``) the upstream's
    ``run_server()`` activates streamable-http via the FastMCP OAuth
    Proxy (designed for clients to do an OAuth dance).
  - We are gateway-fronted with a worker shared secret; the model
    cannot do an OAuth flow. So we deliberately leave OAUTH_* vars
    unset and bypass ``run_server()`` entirely. We import the FastMCP
    instance directly and call ``mcp.run(transport="streamable-http",
    host=..., port=..., path=...)`` with explicit kwargs — fastmcp 3.x
    defaults the path to ``"/mcp/"`` and the port to 8000, so we must
    override both for the gateway to reach us at the expected URL.
  - Auth uses google-ads.yaml (static creds: developer_token, client_id,
    client_secret, refresh_token, login_customer_id). We materialize
    the yaml from env vars on container start and point the
    google-ads-python lib at it via
    ``GOOGLE_ADS_CONFIGURATION_FILE_PATH``.

This sidesteps the original tunnel-zombie problem by removing the
supergateway --stateless respawn-storm entirely (single in-process
FastMCP, persistent gRPC channels).
"""
from __future__ import annotations

import os
import sys


def _materialize_google_ads_yaml() -> None:
    """Build /tmp/google-ads.yaml from env vars and point the SDK at it.

    The google-ads-python client reads its config from a yaml file at
    ``GOOGLE_ADS_CONFIGURATION_FILE_PATH`` (or ~/google-ads.yaml by default).
    We accept the same env vars the previous .env already has so no
    credential rotation is needed for the cutover.
    """
    required = {
        "GOOGLE_ADS_DEVELOPER_TOKEN": "developer_token",
        "GOOGLE_ADS_OAUTH_CLIENT_ID": "client_id",
        "GOOGLE_ADS_OAUTH_CLIENT_SECRET": "client_secret",
        "GOOGLE_ADS_OAUTH_REFRESH_TOKEN": "refresh_token",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "login_customer_id",
    }
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing required env vars for google-ads.yaml: {missing}"
        )

    yaml_path = "/tmp/google-ads.yaml"
    with open(yaml_path, "w", encoding="utf-8") as f:
        for env_name, yaml_key in required.items():
            value = os.environ[env_name]
            # Quote with single quotes; refresh tokens have / and = which yaml
            # reads fine but quoting removes any ambiguity.
            f.write(f"{yaml_key}: '{value}'\n")
        f.write("use_proto_plus: True\n")
    os.chmod(yaml_path, 0o600)
    os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = yaml_path


def main() -> None:
    _materialize_google_ads_yaml()

    # Importing ads_mcp.server triggers ``from ads_mcp.tools import ...`` and
    # ``from ads_mcp.resources import ...``, which register all handlers on
    # the FastMCP instance at ads_mcp.coordinator.mcp. We do NOT call
    # run_server() — that selects stdio when OAuth env vars are absent and
    # we want streamable-http unconditionally.
    import ads_mcp.server  # noqa: F401
    from ads_mcp.coordinator import mcp

    # FastMCP.run() drives an internal event loop, blocks until shutdown.
    # Explicit kwargs:
    #   host="0.0.0.0" — listen on all docker bridge interfaces (default
    #     127.0.0.1 would reject the "google_ads_mcp:8080" hostname the
    #     gateway uses).
    #   port=8080 — match the gateway's UPSTREAM_GOOGLE_ADS URL.
    #   path="/" — the gateway strips "/mcp/google-ads" before forwarding,
    #     so the upstream must serve at root. Default in fastmcp 3.x is
    #     "/mcp/", which would 404 / 502 on every request.
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8080,
        path="/",
    )


if __name__ == "__main__":
    main()
