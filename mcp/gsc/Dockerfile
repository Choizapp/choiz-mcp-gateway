# GSC MCP — Google Search Console (search_analytics, list_sites,
# get_sitemap, index_inspect, ...).
#
# Source: public npm package `mcp-server-gsc` (ahonn/mcp-server-gsc), no
# Choiz fork. Installed at image build via `npm install -g`.
#
# Stack: Node MCP that natively speaks stdio. We wrap it with supergateway
# (default --stateless mode) so the container exposes MCP Streamable HTTP
# on :8080.
#
# Multi-tenant pattern: TWO containers (gsc_choiz_mcp + gsc_timeless_mcp)
# differing only in route slug. The package itself has NO site-filtering
# env var; both services run identical configs. Tenant separation is
# purely a UX convention via the connector slug — claude.ai picks the
# right `siteUrl` per-tool-call based on which connector the user chose.
# The shared service-account (ga4-mcp-reader, also used by GA4) has
# access to both Choiz and Timeless GSC sites.
#
# Auth: same service-account JSON as GA4. Reused via the existing
# `GA4_SERVICE_ACCOUNT_JSON_B64` env var on the EC2 .env, surfaced into
# this container as `GSC_SERVICE_ACCOUNT_JSON_B64` (compose-level rename
# so GA4 and GSC entrypoints can evolve independently). Container's
# entrypoint decodes to /app/sa.json before launching, so the JSON does
# not bake into the image layer.
#
# Pin: the package version is unpinned (npm install -g pulls latest at
# build time). If the package's tool surface changes in a way that
# breaks our smoke test, pin to a specific version with @x.y.z.

FROM node:20-slim

# ca-certificates: HTTPS to searchconsole.googleapis.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install both the GSC MCP server and supergateway in one shot.
RUN npm install -g mcp-server-gsc supergateway

WORKDIR /app

# Default credentials path — set here so mcp-server-gsc finds the SA file
# without the entrypoint having to export it.
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/sa.json

# Custom entrypoint: decodes GSC_SERVICE_ACCOUNT_JSON_B64 to /app/sa.json,
# then exec's supergateway with the full CMD args. Done at container
# start (not at build) so the JSON does not bake into the image.
RUN printf '%s\n' \
 '#!/bin/sh' \
 'set -e' \
 'if [ -z "${GSC_SERVICE_ACCOUNT_JSON_B64:-}" ]; then' \
 '  echo "ERROR: GSC_SERVICE_ACCOUNT_JSON_B64 not set" >&2' \
 '  exit 1' \
 'fi' \
 'echo "$GSC_SERVICE_ACCOUNT_JSON_B64" | base64 -d > /app/sa.json' \
 'chmod 600 /app/sa.json' \
 'exec supergateway "$@"' \
 > /entrypoint.sh \
 && chmod +x /entrypoint.sh

EXPOSE 8080

# `mcp-server-gsc` is a stdio MCP server. supergateway wraps it as
# Streamable HTTP on :8080. --streamableHttpPath / matters: the gateway
# strips /mcp/<name> before forwarding.
ENTRYPOINT ["/entrypoint.sh"]
CMD [ \
  "--stdio", "mcp-server-gsc", \
  "--outputTransport", "streamableHttp", \
  "--port", "8080", \
  "--streamableHttpPath", "/" \
]
