# Instagram MCP (Graph API for Instagram Business accounts).
#
# Source: https://github.com/Choizapp/choiz-instagram-mcp (Choiz fork of
# jlbadano/ig-mcp with Graph API v23 metric updates: 'impressions' is
# deprecated in v22+, replaced by 'views'; 'follows' and 'total_interactions'
# added).
# Stack: Python; src/instagram_mcp_server.py uses the low-level mcp.server.Server
# (NOT FastMCP) and runs its own asyncio.run(main()) on stdio. Tools return
# Sequence[TextContent], so the str-return primitive bug we hit on facebook
# does NOT apply here.
# We wrap with supergateway so the container exposes MCP Streamable HTTP on
# :8080, which is what claude.ai speaks.
#
# A SINGLE image is built from this Dockerfile and reused by both
# instagram_choiz_mcp and instagram_timeless_mcp services. The two tenants
# differ only in env vars (INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_BUSINESS_ACCOUNT_ID).
# FACEBOOK_APP_ID / FACEBOOK_APP_SECRET are shared (one Choiz Facebook app).
#
# The SHA below pins the exact build. Bump it intentionally when you want to
# pick up upstream changes, then push to master — CI/CD rebuilds and deploys.

FROM python:3.12-slim

ARG INSTAGRAM_MCP_SHA=c233cd6696e4b06ac28eea2dc2378d8b046d3e10

# git: fetch source at build time.
# nodejs/npm: install supergateway (the stdio <-> Streamable HTTP bridge).
# ca-certificates: HTTPS to graph.facebook.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN git clone https://github.com/Choizapp/choiz-instagram-mcp.git . \
 && git checkout "${INSTAGRAM_MCP_SHA}"

# Upstream pins all runtime deps in requirements.txt (mcp, fastmcp, httpx,
# pydantic, pydantic-settings, structlog, etc). Keeping the install verbatim
# so we don't drift from what the fork's authors test against.
RUN pip install --no-cache-dir -r requirements.txt

# supergateway bridges stdio <-> Streamable HTTP (same wrapper used by facebook/meta-ads).
RUN npm install -g supergateway

# Disable Python stdio buffering so supergateway sees the MCP's initial
# handshake immediately. Critical for the Streamable HTTP wrapper.
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# `python -m src.instagram_mcp_server` is the launcher: the module's __main__
# block runs asyncio.run(main()) which starts the stdio_server. supergateway
# captures that stdio and exposes it as Streamable HTTP on :8080.
# (Different from facebook, which uses `mcp run` because its server.py only
# defines the FastMCP instance without calling .run() itself.)
# --streamableHttpPath / matters: the gateway strips /mcp/<name> before
# forwarding, so the upstream must serve at /.
ENTRYPOINT ["supergateway"]
CMD [ \
  "--stdio", "python -m src.instagram_mcp_server", \
  "--outputTransport", "streamableHttp", \
  "--port", "8080", \
  "--streamableHttpPath", "/" \
]
