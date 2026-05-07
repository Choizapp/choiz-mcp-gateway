# GA4 MCP — Google Analytics 4 reporting (run_report, run_funnel_report,
# get_account_summaries, get_property_details, run_realtime_report,
# get_custom_dimensions_and_metrics, list_google_ads_links).
#
# Source: https://github.com/googleanalytics/google-analytics-mcp — the
# **official** Google Analytics MCP, replacing the previous Choiz fork at
# Choizapp/choiz-ga4-mcp@a0278ff (cutover 2026-05-07).
#
# Stack: Python; uses mcp.server.lowlevel.Server with hardcoded stdio
# transport (analytics_mcp/server.py:run_server_async). We do NOT launch
# the upstream `analytics-mcp` console script — instead /opt/entrypoint.py
# imports analytics_mcp.coordinator.app (the Server instance) and serves
# it directly through StreamableHTTPSessionManager + Starlette + uvicorn.
# Same shape as mcp/instagram/.
#
# Multi-tenant: one image, two service blocks (ga4_choiz, ga4_timeless)
# differing only in slug. The official MCP takes property_id as a tool
# argument (not env), so both tenants point at the same code with the
# same auth; the model passes the right property_id per call.
#
# Auth: GOOGLE_APPLICATION_CREDENTIALS file path. The Choiz reader SA JSON
# is provided through the GA4_SERVICE_ACCOUNT_JSON_B64 env var; the
# entrypoint base64-decodes it to /tmp/ga4-sa.json on each container
# start (not at build time, so the JSON does not bake into the image).
#
# Pin to commit 6fd3bf88 of the upstream main branch for reproducibility.

FROM python:3.12-slim

ARG GA4_MCP_SHA=6fd3bf88e2591855cf3fade7e3c791e1f6bda72d

# git: pip install from a git URL needs git in the image at install time.
# ca-certificates: HTTPS to analyticsdata.googleapis.com + GitHub.
# build-essential: some transitive deps (e.g. grpcio) build wheels on ARM64.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install the official MCP from a pinned commit. pyproject.toml registers
# the analytics_mcp package + console scripts, but we don't use the script
# (entrypoint.py imports the package directly).
RUN pip install --no-cache-dir \
    "git+https://github.com/googleanalytics/google-analytics-mcp.git@${GA4_MCP_SHA}" \
    "uvicorn[standard]" \
    starlette

# Strip build deps once wheels are compiled to keep the final layer slim.
RUN apt-get purge -y --auto-remove build-essential && rm -rf /var/lib/apt/lists/*

# Flush stdout immediately so init logs land in `docker logs` without delay.
ENV PYTHONUNBUFFERED=1

COPY entrypoint.py /opt/entrypoint.py

EXPOSE 8080

# entrypoint.py decodes GA4_SERVICE_ACCOUNT_JSON_B64 → /tmp/ga4-sa.json,
# sets GOOGLE_APPLICATION_CREDENTIALS, imports the official package's
# coordinator.app, and serves it via streamable-http on 0.0.0.0:8080.
# No supergateway, no Node, no per-request child churn.
ENTRYPOINT ["python", "/opt/entrypoint.py"]
