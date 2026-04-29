# GA4 MCP — Google Analytics 4 reporting (run_report, schema browsing).
#
# Source: https://github.com/Choizapp/choiz-ga4-mcp (Choiz fork, Google
# Analytics 4 reporting on top of google-analytics-data v1beta).
#
# Stack: Python; ga4_mcp/server.py defines a FastMCP instance and calls
# mcp.run(transport="stdio") at the end of main(). Launcher: `python -m
# ga4_mcp` — the package's __main__ wires it up.
#
# Multi-tenant: one image, two service blocks (ga4_choiz, ga4_timeless)
# differing only in GA4_PROPERTY_ID. Service-account JSON is shared
# between both tenants.
#
# Auth: service-account JSON. Two ways to get it into the container:
#   (a) GA4_SERVICE_ACCOUNT_JSON_B64 env var (single-line base64) — what
#       we use. Entrypoint decodes to /app/sa.json before launching.
#   (b) Mounted file via Docker secret — rejected as overkill; the env
#       pattern matches the rest of our MCPs.
#
# We wrap with supergateway (--stateful) so the python child + the
# gRPC BetaAnalyticsDataClient persist across MCP requests on the same
# session. The default --stateless mode would respawn the python child
# per request, importing the heavy google-analytics-data gRPC stack and
# opening fresh outbound channels each call — same connection-storm
# pattern that destabilized the cloudflared tunnel under google-ads on
# 2026-04-28. --stateful sidesteps that.
#
# The SHA below pins the exact build. Bump it intentionally when you
# want to pick up upstream changes; CI/CD rebuilds and deploys.

FROM python:3.12-slim

ARG GA4_MCP_SHA=a0278ff

# git: clone source at build time.
# nodejs/npm: install supergateway (the stdio <-> Streamable HTTP bridge).
# ca-certificates: HTTPS to analyticsdata.googleapis.com (gRPC over TLS).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN git clone https://github.com/Choizapp/choiz-ga4-mcp.git . \
 && git checkout "${GA4_MCP_SHA}"

RUN pip install --no-cache-dir -r requirements.txt

# supergateway bridges stdio <-> Streamable HTTP. --stateful keeps one
# python child per MCP session (see comment above).
RUN npm install -g supergateway

# Disable Python stdio buffering so supergateway sees the MCP's initial
# handshake immediately. Critical for the Streamable HTTP wrapper.
ENV PYTHONUNBUFFERED=1

# Default credentials path — set here so the python process picks it up
# without the entrypoint having to export it.
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/sa.json

# Custom entrypoint: decodes GA4_SERVICE_ACCOUNT_JSON_B64 to /app/sa.json,
# then exec's supergateway with the full CMD args. Done at container
# start (not at build) so the JSON does not bake into the image layer.
RUN printf '%s\n' \
 '#!/bin/sh' \
 'set -e' \
 'if [ -z "${GA4_SERVICE_ACCOUNT_JSON_B64:-}" ]; then' \
 '  echo "ERROR: GA4_SERVICE_ACCOUNT_JSON_B64 not set" >&2' \
 '  exit 1' \
 'fi' \
 'echo "$GA4_SERVICE_ACCOUNT_JSON_B64" | base64 -d > /app/sa.json' \
 'chmod 600 /app/sa.json' \
 'exec supergateway "$@"' \
 > /entrypoint.sh \
 && chmod +x /entrypoint.sh

EXPOSE 8080

# `python -m ga4_mcp` runs ga4_mcp/__main__.py which calls server.main().
# server.main() calls mcp.run(transport="stdio") on the FastMCP instance.
# supergateway captures that stdio and exposes it as Streamable HTTP on :8080.
# --streamableHttpPath / matters: the gateway strips /mcp/<name> before
# forwarding, so the upstream must serve at /.
#
# NOTE: --stateful was tried first (commit before this) on the assumption
# that the gRPC connection-storm pattern from google-ads would also bite
# GA4. In practice supergateway's stateful path tripped issue #126
# ("Conflict: Only one SSE stream is allowed per session") on every
# claude.ai session, SIGTERMing the python child after ~1 tool call.
# Reverted to default --stateless. The google-analytics-data SDK is much
# lighter than google-ads (no protobuf descriptor pool, single channel
# per request), so the connection-storm hypothesis is unlikely to
# reproduce. If it does, the auto-reboot escalation in monitor-tunnel.yml
# will recover the host without manual intervention.
ENTRYPOINT ["/entrypoint.sh"]
CMD [ \
  "--stdio", "python -m ga4_mcp", \
  "--outputTransport", "streamableHttp", \
  "--port", "8080", \
  "--streamableHttpPath", "/" \
]
