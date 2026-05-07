"""Google Ads MCP entrypoint — official googleads/google-ads-mcp wrapped in
streamable-http with static OAuth user creds via ADC "authorized_user" JSON.

Cutover from the previous Choiz fork (Choizapp/choiz-google-ads-mcp@6fefe68,
disabled 2026-04-28 due to tunnel-zombie pattern under supergateway --stateless)
to the official MCP at https://github.com/googleads/google-ads-mcp pinned to
commit 2a09ac31.

Auth (the tricky part):
  The official MCP's `ads_mcp/utils.py:_create_credentials()` only supports
  two paths:
    1. FastMCP's `get_access_token()` (active only when the OAuth Proxy
       is configured via GOOGLE_ADS_MCP_OAUTH_CLIENT_ID/SECRET — not our
       case, gateway-fronted).
    2. `google.auth.default(scopes=[ADS_SCOPE])` — Application Default
       Credentials.
  It deliberately does NOT support `google-ads.yaml` or env-var fallback
  to a refresh_token directly.

  Workaround: ADC supports a "authorized_user" credential type, which is
  exactly the shape `gcloud auth application-default login` produces — a
  JSON file holding {client_id, client_secret, refresh_token,
  type: "authorized_user"}. We construct that file from our existing
  .env values and point GOOGLE_APPLICATION_CREDENTIALS at it. The
  google-auth library then mints access tokens from the refresh token
  on demand. Same scope (https://www.googleapis.com/auth/adwords) the
  refresh token was originally issued for, so no re-minting needed.

Other env (read directly by ads_mcp/utils.py):
  - GOOGLE_ADS_DEVELOPER_TOKEN (required)
  - GOOGLE_ADS_LOGIN_CUSTOMER_ID (optional; we set it to the MCC)

Transport:
  - The upstream uses the standalone `fastmcp` package (gofastmcp.com,
    >=3.x), NOT `mcp.server.fastmcp.FastMCP` from the official MCP SDK.
    Different defaults, different override surface — see
    feedback_two_fastmcp_packages.md.
  - We bypass run_server() (which would pick stdio without OAuth env)
    and call mcp.run() with explicit host/port/path kwargs.
"""
from __future__ import annotations

import json
import os


def _materialize_adc_credentials() -> None:
    """Build a `gcloud auth application-default login`-style JSON file.

    google.auth.default() reads the path in GOOGLE_APPLICATION_CREDENTIALS,
    detects type="authorized_user", and constructs Credentials that
    auto-refresh from the embedded refresh_token. The MCP code then sees
    valid ADC and skips the "default credentials not found" error path.
    """
    required = {
        "GOOGLE_ADS_OAUTH_CLIENT_ID": "client_id",
        "GOOGLE_ADS_OAUTH_CLIENT_SECRET": "client_secret",
        "GOOGLE_ADS_OAUTH_REFRESH_TOKEN": "refresh_token",
    }
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing OAuth env vars for ADC authorized_user JSON: {missing}"
        )

    adc = {yaml_key: os.environ[env_name] for env_name, yaml_key in required.items()}
    adc["type"] = "authorized_user"

    adc_path = "/tmp/google-ads-adc.json"
    with open(adc_path, "w", encoding="utf-8") as f:
        json.dump(adc, f)
    os.chmod(adc_path, 0o600)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = adc_path

    # Sanity-check the developer token is set (read directly by the MCP).
    if not os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"):
        raise RuntimeError("GOOGLE_ADS_DEVELOPER_TOKEN is required.")


def main() -> None:
    _materialize_adc_credentials()

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
