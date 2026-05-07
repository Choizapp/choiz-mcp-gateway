# Google Ads MCP — official googleads/google-ads-mcp.
#
# Source: https://github.com/googleads/google-ads-mcp — the **official** MCP
# from the Google Ads team, replacing the previous Choiz fork
# (Choizapp/choiz-google-ads-mcp@6fefe68) that was disabled on 2026-04-28
# because supergateway --stateless respawn-storms destabilized the
# cloudflared tunnel (see project_google_ads_destabilizes_host_CONFIRMED).
#
# This image fixes the tunnel-zombie root cause structurally: there is no
# supergateway, no per-request Python child churn, no gRPC channel storm.
# A single FastMCP process holds the GoogleAdsClient and serves all
# requests in-band via mcp.server.streamable_http.
#
# Stack: Python; FastMCP at ads_mcp.coordinator.mcp. The upstream's
# run_server() picks stdio when no OAuth env vars are set; we bypass that
# and run streamable-http directly via mcp/google-ads/entrypoint.py with
# the warehouse-style FastMCP.__init__ monkey-patch.
#
# Auth: static creds (developer_token + OAuth refresh token + login
# customer id) materialized into /tmp/google-ads.yaml at container start
# from env vars. Same env contract as the previous fork — zero credential
# rotation needed for the cutover.
#
# Single tenant: Choiz only (MCC 3916621621). The login_customer_id is a
# manager account; the model passes the specific customer_id per tool
# call (search, get_resource_metadata, etc.).
#
# Pin to commit 2a09ac31 of the upstream main branch for reproducibility.

FROM python:3.12-slim

ARG GOOGLE_ADS_MCP_SHA=2a09ac31d76b9dab7b02a325faa192dd31e2606e

# git: pip install from a git URL needs git in the image at install time.
# ca-certificates: HTTPS to googleads.googleapis.com + GitHub.
# build-essential: grpcio + some google-ads transitive deps build wheels
# on ARM64.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install the official MCP from a pinned commit. pyproject.toml registers
# the ads_mcp package + console scripts; we don't use the script
# (entrypoint.py imports the package directly).
RUN pip install --no-cache-dir \
    "git+https://github.com/googleads/google-ads-mcp.git@${GOOGLE_ADS_MCP_SHA}"

# Strip build deps once wheels are compiled to keep the final layer slim.
RUN apt-get purge -y --auto-remove build-essential && rm -rf /var/lib/apt/lists/*

# Flush stdout immediately so init logs land in `docker logs` without delay.
ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

# entrypoint.py builds /tmp/google-ads.yaml from env vars, applies the
# FastMCP.__init__ monkey-patch, imports ads_mcp.server (registers all
# tools), and runs mcp.run(transport="streamable-http"). No supergateway,
# no Node, no per-request child spawning.
ENTRYPOINT ["python", "/opt/entrypoint.py"]
